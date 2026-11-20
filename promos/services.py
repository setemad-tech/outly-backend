"""
promos/services.py
-------------------
Checks a promo code against an order and works out the discount. Called
from bookings/quote.py only, so the checkout panel and the actual charge
can't disagree.

Rules:
  - percentage codes come off the eligible lines' price, rounded DOWN to a
    whole AED; fixed codes take that many AED off, up to the eligible total
  - max_discount_minor caps either kind; the result is never negative, and
    anything left to pay by card is either 0 or at least AED 2 (Stripe)
  - the discount is split across eligible lines in proportion to their price
  - an organizer-funded discount shrinks OUTLY's service fee in the same
    proportion (the fee stays 15% of what was really paid); an OUTLY-funded
    one leaves the organizer's share untouched and is recorded in
    Booking.outly_discount_minor
"""

from events.pricing import MIN_CARD_CHARGE_MINOR
from .models import PromoCode

_AED = 100


class PromoError(Exception):
    pass


def live_uses(promo, user=None):
    """Orders currently using the code — paid ones and checkouts within the hold."""
    from events.models import live_bookings
    from bookings.models import Booking

    qs = live_bookings(Booking.objects.filter(promo_code=promo))
    if user is not None:
        qs = qs.filter(user=user)
    return qs.count()


def _aed(minor):
    return f"{minor // _AED} AED"


def find_code(raw, *, lock=False):
    from .models import normalize_code

    code = normalize_code(raw)
    qs = PromoCode.objects.all()
    if lock:
        qs = qs.select_for_update()
    return qs.filter(code=code).first()


def apply_promo(quote, *, event, user, now):
    """Fills quote.promo / discount_minor / line discounts, or raises PromoError."""
    # Locked so two checkouts can't both take the last redemption; only
    # matters inside a transaction (booking time), harmless otherwise.
    from django.db import connection

    promo = find_code(quote.promo_code, lock=connection.in_atomic_block)
    if promo is None:
        raise PromoError("That promo code doesn't exist.")
    if not promo.is_active:
        raise PromoError("That promo code is paused.")
    if promo.starts_at and now < promo.starts_at:
        raise PromoError("That promo code isn't active yet.")
    if promo.ends_at and now >= promo.ends_at:
        raise PromoError("That promo code has expired.")
    if promo.organizer_id and promo.organizer_id != event.organizer_id:
        raise PromoError("That promo code isn't valid for this event.")
    if promo.event_id and promo.event_id != event.id:
        raise PromoError("That promo code isn't valid for this event.")
    if promo.min_level:
        from loyalty.levels import LEVEL_NAMES, meets
        if not meets(quote.level, promo.min_level):
            raise PromoError(
                "That promo code is only for Icon members." if promo.min_level == "icon"
                else f"That promo code is for {LEVEL_NAMES[promo.min_level]} members and above."
            )

    allowed_types = set(promo.ticket_types.values_list("id", flat=True))
    eligible = [
        l for l in quote.lines
        if l.ticket_type.allow_promo and l.total_minor > 0
        and (not allowed_types or l.ticket_type.id in allowed_types)
    ]
    if not eligible:
        raise PromoError("That promo code doesn't apply to these tickets.")
    eligible_total = sum(l.total_minor for l in eligible)
    if promo.min_subtotal_minor and eligible_total < promo.min_subtotal_minor:
        raise PromoError(f"Spend at least {_aed(promo.min_subtotal_minor)} to use that promo code.")

    if promo.max_redemptions is not None and live_uses(promo) >= promo.max_redemptions:
        raise PromoError("That promo code has been fully used.")
    if user is not None and getattr(user, "is_authenticated", False):
        if live_uses(promo, user) >= promo.per_user_limit:
            raise PromoError("You've already used that promo code.")

    if promo.kind == PromoCode.Kind.PERCENT:
        discount = eligible_total * min(promo.value, 100) // 100
    else:
        discount = promo.value
    if promo.max_discount_minor is not None:
        discount = min(discount, promo.max_discount_minor)
    discount = min(discount, eligible_total)
    discount -= discount % _AED  # whole AED, like every price on OUTLY

    left = quote.subtotal_minor - discount
    if 0 < left < MIN_CARD_CHARGE_MINOR:
        discount = quote.subtotal_minor - MIN_CARD_CHARGE_MINOR
    if discount <= 0:
        raise PromoError("That promo code doesn't lower the price of these tickets.")

    # Split across eligible lines by price; the rounding remainder goes on the last one.
    remaining = discount
    for i, line in enumerate(eligible):
        share = remaining if i == len(eligible) - 1 else discount * line.total_minor // eligible_total
        share = min(share, line.total_minor, remaining)
        line.discount_minor = share
        remaining -= share
        if promo.funded_by == PromoCode.FundedBy.ORGANIZER and share:
            line.service_fee_minor -= line.service_fee_minor * share // line.total_minor

    quote.promo = promo
    quote.discount_minor = discount
    if promo.funded_by == PromoCode.FundedBy.OUTLY:
        quote.outly_discount_minor = discount
