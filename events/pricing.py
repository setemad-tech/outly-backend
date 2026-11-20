"""
events/pricing.py
-------------------
The one place ticket prices and loyalty points are worked out.

Event.price_minor is the organizer's own price. Attendees pay that plus
the organizer's service charge (OrganizerProfile.service_fee_percent, set by
admin; SERVICE_FEE_PERCENT is the default) for VAT, tools and service costs, rounded UP to a whole
AED — prices never show decimals. Buyers with a loyalty level (Explorer and
up, loyalty/levels.py) also earn LOYALTY_PERCENT of the organizer's price in
points, rounded to the nearest whole point; level 0 earns none.

    organizer price 75 AED -> 75 * 1.15 = 86.25 -> attendee pays 87 AED
                              75 * 0.02 = 1.5   -> 2 points (Explorer and up)

Points are spent 1 point = 1 AED off at checkout (redeemable_points).

Everything is integer maths on minor units (fils), so no float rounding.
The frontend mirrors this in src/lib/pricing.ts for the event editor's live
preview — keep the two in step.
"""

SERVICE_FEE_PERCENT = 15
LOYALTY_PERCENT = 2  # owner, 2026-11-20 (was 4%)

# Redeeming: 1 point = 1 AED off at checkout. Stripe won't take a card
# payment under AED 2, so points either cover the whole order or leave at
# least this much to pay by card.
FILS_PER_POINT = 100
MIN_CARD_CHARGE_MINOR = 200

_FILS_PER_AED = 100


def fee_percent_for(event) -> int:
    """The service charge for this event's organizer (admin sets it per organizer)."""
    pct = getattr(getattr(event, "organizer", None), "service_fee_percent", None)
    return SERVICE_FEE_PERCENT if pct is None else pct


def ticket_price_minor(base_minor: int, fee_percent: int = SERVICE_FEE_PERCENT) -> int:
    """What one ticket costs the attendee, in fils (always whole AED)."""
    if base_minor <= 0:
        return 0
    with_fee = base_minor * (100 + fee_percent)  # fils x 100
    whole_aed = -(-with_fee // (100 * _FILS_PER_AED))  # ceiling division
    return whole_aed * _FILS_PER_AED


def service_fee_minor(base_minor: int, fee_percent: int = SERVICE_FEE_PERCENT) -> int:
    """OUTLY's share of one ticket: the service charge plus the round-up."""
    return ticket_price_minor(base_minor, fee_percent) - max(base_minor, 0)


def loyalty_points(base_minor: int, percent: int = LOYALTY_PERCENT) -> int:
    """Points one ticket earns: `percent` of the organizer's price, nearest whole point (.5 rounds up)."""
    if base_minor <= 0 or percent <= 0:
        return 0
    scaled = base_minor * percent  # = points x 100 x 100
    unit = 100 * _FILS_PER_AED
    return (scaled + unit // 2) // unit


def redeemable_points(total_minor: int, balance: int) -> int:
    """How many of `balance` points can go towards an order of `total_minor` fils."""
    if total_minor <= 0 or balance <= 0:
        return 0
    covers_all = -(-total_minor // FILS_PER_POINT)
    if balance >= covers_all:
        return covers_all  # the whole order, nothing left to pay
    # Partial: keep the card charge at or above Stripe's minimum.
    return max(0, min(balance, (total_minor - MIN_CARD_CHARGE_MINOR) // FILS_PER_POINT))
