"""
bookings/quote.py
------------------
Prices an order — ticket types x quantities, promo code, loyalty points —
in exactly one place. The checkout panel shows this quote (POST
/api/events/<id>/quote/) and create_pending_booking charges it, so the total
a buyer sees is the total they pay (BU-08: server-authoritative quote).

Order of operations (the stacking policy, shown the same way in the UI):
  1. each line: units x (organizer price + service fee), whole AED
  2. promo code off the lines it applies to
  3. loyalty points off what's left (1 point = 1 AED), on lines that allow them
  4. points earned: only on what's left to pay by card
Stripe can't take a card payment under AED 2, so whatever's left to pay is
either 0 or at least that.

All money is integer fils.
"""

from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.utils import timezone

from events.models import TicketType
from events.pricing import (
    FILS_PER_POINT, MIN_CARD_CHARGE_MINOR, loyalty_points, redeemable_points,
    fee_percent_for, service_fee_minor, ticket_price_minor,
)

# Most units (tickets or packages) in one order.
MAX_UNITS_PER_ORDER = 10


class QuoteError(ValidationError):
    """The tickets asked for can't be sold (not the promo code — see Quote.promo_error)."""


class SoldOutError(QuoteError):
    pass


@dataclass
class Line:
    ticket_type: TicketType
    quantity: int
    unit_base_minor: int
    unit_price_minor: int
    service_fee_minor: int  # whole line, after any organizer-funded promo share
    loyalty_points: int     # whole line
    discount_minor: int = 0

    @property
    def guests(self):
        return self.quantity * self.ticket_type.guests_per_unit

    @property
    def total_minor(self):
        return self.unit_price_minor * self.quantity


@dataclass
class Quote:
    lines: list
    subtotal_minor: int = 0
    discount_minor: int = 0
    outly_discount_minor: int = 0
    promo: object = None          # PromoCode, when one applied
    promo_code: str = ""          # as entered, normalised
    promo_error: str = ""         # why the entered code didn't apply
    points_redeemed: int = 0
    points_balance: int = 0
    points_usable: int = 0        # what this order could take, whether used or not
    total_minor: int = 0          # left to pay by card
    loyalty_points: int = 0       # earned once paid
    warnings: list = field(default_factory=list)
    level: str = ""               # buyer's loyalty level (members-only codes, early access)

    @property
    def guests(self):
        return sum(l.guests for l in self.lines)

    @property
    def units(self):
        return sum(l.quantity for l in self.lines)

    @property
    def service_fee_minor(self):
        return sum(l.service_fee_minor for l in self.lines)

    def as_dict(self):
        return {
            "lines": [
                {
                    "ticket_type": str(l.ticket_type.id),
                    "name": l.ticket_type.name,
                    "quantity": l.quantity,
                    "guests_per_unit": l.ticket_type.guests_per_unit,
                    "guests": l.guests,
                    "unit_price_minor": l.unit_price_minor,
                    "total_minor": l.total_minor,
                    "discount_minor": l.discount_minor,
                }
                for l in self.lines
            ],
            "units": self.units,
            "guests": self.guests,
            "subtotal_minor": self.subtotal_minor,
            "promo_code": self.promo_code if self.promo else "",
            "promo_label": self.promo.describe() if self.promo else "",
            "promo_error": self.promo_error,
            "discount_minor": self.discount_minor,
            "points_balance": self.points_balance,
            "points_usable": self.points_usable,
            "points_redeemed": self.points_redeemed,
            "total_minor": self.total_minor,
            "loyalty_points": self.loyalty_points,
            "currency": "AED",
        }


def parse_items(raw):
    """[{"ticket_type": id, "quantity": n}, ...] -> {id: n}, merged, zeros dropped."""
    if not isinstance(raw, list):
        raise QuoteError("Choose at least one ticket.")
    wanted = {}
    for row in raw:
        if not isinstance(row, dict):
            raise QuoteError("Invalid ticket selection.")
        try:
            qty = int(row.get("quantity", 0))
        except (TypeError, ValueError):
            raise QuoteError("Invalid number of tickets.")
        if qty < 0:
            raise QuoteError("Invalid number of tickets.")
        key = str(row.get("ticket_type", ""))
        if qty:
            wanted[key] = wanted.get(key, 0) + qty
    if not wanted:
        raise QuoteError("Choose at least one ticket.")
    return wanted


def default_items(event, quantity, user=None):
    """Old clients send only `quantity`: that many of the event's first ticket type on sale."""
    from loyalty.levels import user_level

    now = timezone.now()
    spots = event.spots_left
    level = user_level(user)
    for tt in event.ticket_types.filter(status=TicketType.Status.ACTIVE):
        if tt.sale_state(now, spots_left=spots, level=level) == "on_sale":
            return [{"ticket_type": str(tt.id), "quantity": quantity}]
    first = event.ticket_types.exclude(status=TicketType.Status.ARCHIVED).first()
    if first is None:
        raise QuoteError("This event has no tickets on sale.")
    return [{"ticket_type": str(first.id), "quantity": quantity}]


def build_quote(*, event, items, user=None, use_points=False, promo_code="", promo_required=False):
    """
    Prices `items` for `event`. Raises QuoteError if the tickets can't be
    sold as asked. A bad promo code doesn't raise unless promo_required
    (booking time) — the quote then just carries promo_error and no discount.

    Call inside the event's row lock when the result will be charged:
    stock and capacity are read here.
    """
    wanted = parse_items(items)
    types = {
        str(tt.id): tt
        for tt in event.ticket_types.exclude(status=TicketType.Status.ARCHIVED)
    }
    now = timezone.now()
    spots_left = event.spots_left
    from loyalty.levels import points_percent, user_level
    level = user_level(user)  # early access opens some types sooner for members
    earn_pct = points_percent(level)  # level 0 earns no points
    fee = fee_percent_for(event)  # this organizer's service charge

    lines = []
    for key, qty in wanted.items():
        tt = types.get(key)
        if tt is None:
            raise QuoteError("That ticket type isn't available for this event.")
        state = tt.sale_state(now, spots_left=spots_left, level=level)
        if state == "paused":
            raise QuoteError(f"{tt.name} isn't on sale right now.")
        if state == "not_started":
            raise QuoteError(f"{tt.name} isn't on sale yet.")
        if state == "ended":
            raise QuoteError(f"Sales for {tt.name} have ended.")
        if state == "sold_out":
            raise SoldOutError(f"{tt.name} is sold out.")
        if qty > tt.max_per_order:
            raise QuoteError(f"You can buy up to {tt.max_per_order} of {tt.name} per order.")
        left = tt.units_left(spots_left=spots_left)
        if qty > left:
            raise SoldOutError(f"Only {left} left of {tt.name}.")
        lines.append(Line(
            ticket_type=tt,
            quantity=qty,
            unit_base_minor=tt.price_minor,
            unit_price_minor=ticket_price_minor(tt.price_minor, fee),
            service_fee_minor=service_fee_minor(tt.price_minor, fee) * qty,
            loyalty_points=loyalty_points(tt.price_minor, earn_pct) * qty,
        ))
    lines.sort(key=lambda l: (l.ticket_type.position, l.ticket_type.created_at))

    quote = Quote(lines=lines, level=level)
    if quote.units > MAX_UNITS_PER_ORDER:
        raise QuoteError(f"You can buy up to {MAX_UNITS_PER_ORDER} tickets per order.")
    if quote.guests > spots_left:
        raise SoldOutError(f"Only {spots_left} spot(s) left.")

    quote.subtotal_minor = sum(l.total_minor for l in lines)
    earned_before_promo = sum(l.loyalty_points for l in lines)

    # 2. Promo code
    from promos.models import normalize_code
    from promos.services import PromoError, apply_promo

    quote.promo_code = normalize_code(promo_code)
    if quote.promo_code:
        try:
            apply_promo(quote, event=event, user=user, now=now)
        except PromoError as e:
            if promo_required:
                raise QuoteError(str(e))
            quote.promo_error = str(e)

    # 3. Loyalty points
    after_promo = quote.subtotal_minor - quote.discount_minor
    if user is not None and getattr(user, "is_authenticated", False) and after_promo > 0:
        from loyalty.services import balance
        quote.points_balance = balance(user)
        points_base = sum(l.total_minor - l.discount_minor for l in lines if l.ticket_type.allow_points)
        usable = redeemable_points(points_base, quote.points_balance)
        rest = after_promo - usable * FILS_PER_POINT
        if 0 < rest < MIN_CARD_CHARGE_MINOR:
            usable = max(0, (after_promo - MIN_CARD_CHARGE_MINOR) // FILS_PER_POINT)
        quote.points_usable = usable
        if use_points:
            quote.points_redeemed = usable

    quote.total_minor = max(0, after_promo - quote.points_redeemed * FILS_PER_POINT)

    # Points are earned only on what's paid by card — not on a promo
    # discount, and not on the part paid with points (owner, 2026-11-20).
    # An order paid entirely with points or a code earns nothing.
    _share_points(quote, earned_before_promo)
    return quote


def _share_points(quote, earned_full):
    """Scales earned points by cash paid / subtotal, split across the lines (remainder on the last)."""
    if not quote.subtotal_minor or quote.total_minor >= quote.subtotal_minor:
        quote.loyalty_points = earned_full
        return
    total = earned_full * quote.total_minor // quote.subtotal_minor
    quote.loyalty_points = total
    remaining = total
    for i, line in enumerate(quote.lines):
        if i == len(quote.lines) - 1:
            line.loyalty_points = remaining
        else:
            line.loyalty_points = min(remaining, line.loyalty_points * quote.total_minor // quote.subtotal_minor)
            remaining -= line.loyalty_points
