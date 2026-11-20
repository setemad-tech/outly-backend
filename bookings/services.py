"""
bookings/services.py
---------------------
Booking creation lives here (not in a view) so both the API and, later,
things like an admin action or a "book via chat" flow can reuse the same
race-safe logic instead of duplicating it.
"""

from django.db import transaction
from django.core.exceptions import ValidationError

from events.models import Event
from .models import Booking, BookingItem
from .quote import MAX_UNITS_PER_ORDER, QuoteError, SoldOutError, build_quote, default_items

# Kept for old imports; an order is limited in units (tickets or packages).
MAX_TICKETS_PER_BOOKING = MAX_UNITS_PER_ORDER

__all__ = ["SoldOutError", "QuoteError", "create_pending_booking"]


@transaction.atomic
def create_pending_booking(
    *, user, event_id: str, items=None, quantity=1, use_points=False, promo_code=""
) -> Booking:
    """
    Creates a PENDING booking for `items` ([{"ticket_type": id, "quantity":
    n}, ...]), holding their spots, iff stock and capacity allow. Old
    clients that send only `quantity` get that many of the default type.

    select_for_update() locks the Event row for the duration of this
    transaction, so two simultaneous requests for the last spot (or the
    last table) can't both read "1 left" and both succeed — the second
    request blocks until the first transaction commits (or rolls back),
    then re-checks against up-to-date numbers. Ticket-type stock, event
    capacity and promo redemptions are all checked under that lock; a
    plain check without it is not safe under concurrency.
    """
    if not user.email_verified:
        # The real enforcement point: QR tickets go out by email
        # (bookings/emails.py), so no booking should ever be created for an
        # unconfirmed address, regardless of what the frontend gate did or
        # didn't check. Checked before the row lock below — no reason to
        # take it just to reject the request anyway.
        raise ValidationError(
            "Verify your email before booking — check your inbox for the code."
        )

    event = Event.objects.select_for_update().get(pk=event_id)

    if event.status != Event.Status.PUBLISHED:
        raise ValidationError("This event isn't open for booking.")

    # Same cutoff that drops it from the public list: an ended event's page
    # stays reachable by direct link, but it no longer sells.
    if not Event.objects.filter(pk=event.pk).still_listed().exists():
        raise ValidationError("This event has ended.")

    already_booked = (
        Booking.objects.filter(event=event, user=user)
        .exclude(status=Booking.Status.CANCELLED)
        .exists()
    )
    if already_booked:
        # user already has a live booking for this event (unique constraint) —
        # surface that instead of silently no-op'ing
        raise ValidationError("You already have a booking for this event.")

    if items is None:
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            raise ValidationError("Invalid number of tickets.")
        if not 1 <= quantity <= MAX_UNITS_PER_ORDER:
            raise ValidationError(
                f"You can book between 1 and {MAX_UNITS_PER_ORDER} tickets at a time."
            )
        items = default_items(event, quantity, user)

    if use_points:
        # Lock the user's row so two checkouts at once can't spend the same
        # points twice; the balance is read under that lock (in the quote).
        from django.contrib.auth import get_user_model
        get_user_model().objects.select_for_update().filter(pk=user.pk).first()

    quote = build_quote(
        event=event, items=items, user=user, use_points=use_points,
        promo_code=promo_code, promo_required=True,
    )

    booking = Booking.objects.create(
        event=event,
        user=user,
        quantity=quote.guests,
        status=Booking.Status.PENDING,
        price_paid_minor=quote.total_minor,
        service_fee_minor=quote.service_fee_minor,
        loyalty_points=quote.loyalty_points,
        points_redeemed=quote.points_redeemed,
        promo_code=quote.promo,
        promo_code_text=quote.promo.code if quote.promo else "",
        discount_minor=quote.discount_minor,
        outly_discount_minor=quote.outly_discount_minor,
    )
    BookingItem.objects.bulk_create([
        BookingItem(
            booking=booking,
            ticket_type=l.ticket_type,
            name=l.ticket_type.name,
            description=l.ticket_type.description,
            quantity=l.quantity,
            guests_per_unit=l.ticket_type.guests_per_unit,
            unit_base_minor=l.unit_base_minor,
            unit_price_minor=l.unit_price_minor,
            service_fee_minor=l.service_fee_minor,
            discount_minor=l.discount_minor,
            loyalty_points=l.loyalty_points,
        )
        for l in quote.lines
    ])
    if quote.points_redeemed:
        from loyalty.services import redeem_for_booking
        redeem_for_booking(booking)
    return booking


@transaction.atomic
def cancel_pending_booking(booking: Booking):
    """Used when a checkout session expires or a payment fails. Returns any points spent on it."""
    if booking.status == Booking.Status.PENDING:
        booking.status = Booking.Status.CANCELLED
        booking.save(update_fields=["status"])
        from loyalty.services import return_redeemed
        return_redeemed(booking)


@transaction.atomic
def discard_pending_booking(booking: Booking):
    """Removes a hold whose checkout could never start, giving back any points spent on it."""
    from loyalty.services import return_redeemed
    return_redeemed(booking)
    booking.delete()