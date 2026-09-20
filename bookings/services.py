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
from .models import Booking


class SoldOutError(ValidationError):
    pass


@transaction.atomic
def create_pending_booking(*, user, event_id: str, quantity: int = 1) -> Booking:
    """
    Creates a PENDING booking, holding `quantity` spots, iff capacity allows.

    select_for_update() locks the Event row for the duration of this
    transaction, so two simultaneous requests for the last spot can't both
    read "1 spot left" and both succeed — the second request blocks until
    the first transaction commits (or rolls back), then re-checks capacity
    against up-to-date numbers. This is what actually prevents overselling;
    a plain "if event.spots_left >= quantity" check without the lock is not
    safe under concurrency.
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

    if event.spots_left < quantity:
        raise SoldOutError(f"Only {event.spots_left} spot(s) left.")

    booking, created = Booking.objects.get_or_create(
        event=event,
        user=user,
        defaults={
            "quantity": quantity,
            "status": Booking.Status.PENDING,
            "price_paid_minor": event.price_minor * quantity,
        },
    )
    if not created:
        # user already has a booking for this event (unique constraint) —
        # surface that instead of silently no-op'ing
        raise ValidationError("You already have a booking for this event.")

    return booking


@transaction.atomic
def cancel_pending_booking(booking: Booking):
    """Used when a checkout session expires or a payment fails."""
    if booking.status == Booking.Status.PENDING:
        booking.status = Booking.Status.CANCELLED
        booking.save(update_fields=["status"])