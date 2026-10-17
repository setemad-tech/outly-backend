"""
backoffice/analytics.py
-------------------------
Numbers behind both analytics dashboards:
  - organizer_analytics(): one organizer's own events (organizer page)
  - company_overview():    OUTLY as a whole (admin portal home)

Money is always in minor units (fils). "Sold" means CONFIRMED or ATTENDED
bookings; tickets are counted in people (Booking.quantity), not bookings.

Attendees pay the organizer's price plus OUTLY's service fee
(events/pricing.py). Company numbers use what attendees paid; organizer
numbers use only the organizer's share (paid minus service fee).
"""

from datetime import date, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from accounts.models import OrganizerProfile
from bookings.models import Booking
from events.models import Event
from events.pricing import FILS_PER_POINT, SERVICE_FEE_PERCENT

User = get_user_model()

SOLD = [Booking.Status.CONFIRMED, Booking.Status.ATTENDED]
RANGES = {"7": 7, "30": 30, "90": 90, "365": 365}
DEFAULT_RANGE = "30"


def _organizer_share(prefix=""):
    """
    The organizer's part of a booking: the full ticket value (cash paid plus
    points redeemed — OUTLY absorbs points, not the organizer) minus the
    service fee. prefix e.g. "bookings__" when summing from Event.
    """
    return (
        F(f"{prefix}price_paid_minor")
        + F(f"{prefix}points_redeemed") * FILS_PER_POINT
        - F(f"{prefix}service_fee_minor")
    )


def commission_percent() -> int:
    return int(getattr(settings, "PLATFORM_COMMISSION_PERCENT", 0) or 0)


def parse_range(request):
    """?range=7|30|90|365|all -> (key, start_date or None, end_date)."""
    key = request.query_params.get("range", DEFAULT_RANGE)
    if key != "all" and key not in RANGES:
        key = DEFAULT_RANGE
    today = timezone.localdate()
    start = None if key == "all" else today - timedelta(days=RANGES[key] - 1)
    return key, start, today


def _in_range(field, start, end):
    q = Q(**{f"{field}__date__lte": end})
    if start:
        q &= Q(**{f"{field}__date__gte": start})
    return q


def _pct(part, whole):
    return round(part * 100 / whole, 1) if whole else None


def _daily(rows, start: date, end: date, keys):
    """Turns sparse {day: ...} rows into one entry per day, zeros filled in."""
    by_day = {r["day"]: r for r in rows}
    out, day = [], start
    while day <= end:
        r = by_day.get(day, {})
        out.append({"day": day.isoformat(), **{k: r.get(k) or 0 for k in keys}})
        day += timedelta(days=1)
    return out


def _series_start(start, end, *querysets_with_field):
    """For range=all, start the chart at the earliest data point (max ~2 years)."""
    if start:
        return start
    earliest = end
    for qs, field in querysets_with_field:
        first = qs.order_by(field).values_list(field, flat=True).first()
        if first:
            earliest = min(earliest, timezone.localdate(first))
    return max(earliest, end - timedelta(days=729))


def _sales(bookings):
    agg = bookings.filter(status__in=SOLD).aggregate(
        revenue=Coalesce(Sum("price_paid_minor"), 0),
        organizer_revenue=Coalesce(Sum(_organizer_share()), 0),
        service_fees=Coalesce(Sum("service_fee_minor"), 0),
        loyalty_points=Coalesce(Sum("loyalty_points"), 0),
        points_redeemed=Coalesce(Sum("points_redeemed"), 0),
        tickets=Coalesce(Sum("quantity"), 0),
        orders=Count("id"),
        paid_orders=Count("id", filter=Q(price_paid_minor__gt=0)),
    )
    return agg


# ---------------------------------------------------------------------------
# Organizer
# ---------------------------------------------------------------------------

def organizer_analytics(profile: OrganizerProfile, start, end):
    bookings = Booking.objects.filter(event__organizer=profile)
    in_range = bookings.filter(_in_range("booked_at", start, end))
    sales = _sales(in_range)
    fee_pct = commission_percent()

    series_start = _series_start(start, end, (bookings.filter(status__in=SOLD), "booked_at"))
    rows = (
        bookings.filter(status__in=SOLD, booked_at__date__gte=series_start, booked_at__date__lte=end)
        .annotate(day=TruncDate("booked_at"))
        .values("day")
        .annotate(revenue_minor=Sum(_organizer_share()), tickets=Sum("quantity"))
    )

    now = timezone.now()
    events = (
        Event.objects.filter(organizer=profile)
        .annotate(
            attending=Coalesce(Sum("bookings__quantity", filter=Q(bookings__status__in=SOLD)), 0),
            checked_in=Coalesce(Sum("bookings__checked_in_count", filter=Q(bookings__status__in=SOLD)), 0),
            revenue_minor=Coalesce(Sum(_organizer_share("bookings__"), filter=Q(bookings__status__in=SOLD)), 0),
        )
        .order_by("-start_at")
    )
    event_rows = [
        {
            "id": str(e.id),
            "title": e.title,
            "start_at": e.start_at,
            "capacity": e.capacity,
            "attending": e.attending,
            "checked_in": e.checked_in,
            "revenue_minor": e.revenue_minor,
            "is_past": e.start_at < now,
        }
        for e in events
    ]
    past = [r for r in event_rows if r["is_past"]]
    past_sold = sum(r["attending"] for r in past)
    total_capacity = sum(r["capacity"] for r in event_rows)

    return {
        "currency": "AED",
        "commission_percent": fee_pct,
        "summary": {
            # The organizer's own prices x tickets — OUTLY's service fee is on top, not taken out.
            "revenue_minor": sales["organizer_revenue"],
            "net_revenue_minor": sales["organizer_revenue"] - round(sales["organizer_revenue"] * fee_pct / 100),
            "tickets_sold": sales["tickets"],
            "orders": sales["orders"],
            "avg_order_minor": round(sales["organizer_revenue"] / sales["paid_orders"]) if sales["paid_orders"] else 0,
            "events_total": len(event_rows),
            "events_upcoming": len(event_rows) - len(past),
            # Of everyone who held a ticket to an event that has started, how many showed up.
            "check_in_rate_pct": _pct(sum(r["checked_in"] for r in past), past_sold),
            # Seats sold across all events vs. seats offered.
            "sell_through_pct": _pct(sum(r["attending"] for r in event_rows), total_capacity),
        },
        "revenue_over_time": _daily(rows, series_start, end, ["revenue_minor", "tickets"]),
        "attendance_by_event": event_rows,
    }


# ---------------------------------------------------------------------------
# Company (admin portal)
# ---------------------------------------------------------------------------

def _period_kpis(start, end):
    bookings = Booking.objects.filter(_in_range("booked_at", start, end))
    sales = _sales(bookings)
    return {
        "gmv_minor": sales["revenue"],
        "tickets_sold": sales["tickets"],
        "orders": sales["orders"],
        "new_users": User.objects.filter(_in_range("date_joined", start, end)).count(),
    }


def company_overview(start, end):
    fee_pct = commission_percent()
    bookings = Booking.objects.filter(_in_range("booked_at", start, end))
    sales = _sales(bookings)
    kpis = _period_kpis(start, end)

    previous = None
    if start:
        length = (end - start).days + 1
        previous = _period_kpis(start - timedelta(days=length), start - timedelta(days=1))

    # Checkout conversion on paid events: started checkouts that ended up paid.
    paid_event_bookings = bookings.filter(event__price_minor__gt=0)
    started = paid_event_bookings.count()
    completed = paid_event_bookings.filter(status__in=SOLD).count()

    # Show-up rate for sold tickets to events that have already started.
    attended = Booking.objects.filter(
        _in_range("event__start_at", start, end), event__start_at__lte=timezone.now(), status__in=SOLD
    ).aggregate(sold=Coalesce(Sum("quantity"), 0), came=Coalesce(Sum("checked_in_count"), 0))

    buyers = (
        Booking.objects.filter(status__in=SOLD)
        .values("user")
        .annotate(n=Count("id"))
    )
    buyers_total = buyers.count()
    repeat_buyers = buyers.filter(n__gt=1).count()

    users_total = User.objects.count()
    series_start = _series_start(
        start, end,
        (Booking.objects.filter(status__in=SOLD), "booked_at"),
        (User.objects.all(), "date_joined"),
    )
    sales_rows = (
        Booking.objects.filter(status__in=SOLD, booked_at__date__gte=series_start, booked_at__date__lte=end)
        .annotate(day=TruncDate("booked_at"))
        .values("day")
        .annotate(revenue_minor=Sum("price_paid_minor"), tickets=Sum("quantity"))
    )
    signup_rows = (
        User.objects.filter(date_joined__date__gte=series_start, date_joined__date__lte=end)
        .annotate(day=TruncDate("date_joined"))
        .values("day")
        .annotate(signups=Count("id"))
    )
    sales_by_day = {r["day"]: r for r in sales_rows}
    for r in signup_rows:
        sales_by_day.setdefault(r["day"], {"day": r["day"]})["signups"] = r["signups"]
    series = _daily(sales_by_day.values(), series_start, end, ["revenue_minor", "tickets", "signups"])

    sold = bookings.filter(status__in=SOLD)
    top_events = (
        sold.values("event_id", "event__title", "event__start_at", "event__capacity",
                    "event__organizer__display_name")
        .annotate(tickets=Sum("quantity"), revenue_minor=Sum("price_paid_minor"))
        .order_by("-revenue_minor", "-tickets")[:5]
    )
    top_organizers = (
        sold.values("event__organizer_id", "event__organizer__display_name", "event__organizer__status")
        .annotate(
            tickets=Sum("quantity"),
            revenue_minor=Sum("price_paid_minor"),
            events=Count("event", distinct=True),
        )
        .order_by("-revenue_minor", "-tickets")[:5]
    )
    categories = (
        sold.values("event__category__name")
        .annotate(tickets=Sum("quantity"), revenue_minor=Sum("price_paid_minor"))
        .order_by("-tickets")
    )
    gender = sold.values("user__gender").annotate(people=Count("user", distinct=True))

    return {
        "currency": "AED",
        "commission_percent": fee_pct,
        "service_fee_percent": SERVICE_FEE_PERCENT,
        "summary": {
            **kpis,
            # Service fees, plus any commission on the organizer's share.
            "platform_fees_minor": sales["service_fees"] + round(sales["organizer_revenue"] * fee_pct / 100),
            "loyalty_points_issued": sales["loyalty_points"],
            # Discounts OUTLY gave at checkout (1 point = 1 AED); GMV is cash only.
            "loyalty_points_redeemed": sales["points_redeemed"],
            "avg_order_minor": round(sales["revenue"] / sales["paid_orders"]) if sales["paid_orders"] else 0,
            "paid_orders": sales["paid_orders"],
            "users_total": users_total,
            "email_verified_pct": _pct(User.objects.filter(email_verified=True).count(), users_total),
            "organizers_approved": OrganizerProfile.objects.filter(status=OrganizerProfile.Status.APPROVED).count(),
            "organizers_pending": OrganizerProfile.objects.filter(status=OrganizerProfile.Status.PENDING).count(),
            "events_live": Event.objects.filter(status=Event.Status.PUBLISHED).still_listed().count(),
            "events_created": Event.objects.filter(_in_range("created_at", start, end)).count(),
            "checkout_conversion_pct": _pct(completed, started),
            "check_in_rate_pct": _pct(attended["came"], attended["sold"]),
            "repeat_buyer_pct": _pct(repeat_buyers, buyers_total),
        },
        "previous": previous,
        "series": series,
        "top_events": [
            {
                "id": str(r["event_id"]),
                "title": r["event__title"],
                "organizer": r["event__organizer__display_name"],
                "start_at": r["event__start_at"],
                "capacity": r["event__capacity"],
                "tickets": r["tickets"],
                "revenue_minor": r["revenue_minor"],
            }
            for r in top_events
        ],
        "top_organizers": [
            {
                "id": r["event__organizer_id"],
                "name": r["event__organizer__display_name"],
                "verified": r["event__organizer__status"] == OrganizerProfile.Status.APPROVED,
                "events": r["events"],
                "tickets": r["tickets"],
                "revenue_minor": r["revenue_minor"],
            }
            for r in top_organizers
        ],
        "categories": [
            {"name": r["event__category__name"] or "Uncategorized", "tickets": r["tickets"], "revenue_minor": r["revenue_minor"]}
            for r in categories
        ],
        "audience_gender": {
            (r["user__gender"] or "unknown"): r["people"] for r in gender
        },
    }
