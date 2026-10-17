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
from events.pricing import (
    FILS_PER_POINT, loyalty_points, redeemable_points, service_fee_minor, ticket_price_minor,
)
from .models import Booking


# Per booking, not per event — one person can buy for a group, but not
# sweep up a whole event in a single checkout.
MAX_TICKETS_PER_BOOKING = 10


class SoldOutError(ValidationError):
    pass


@transaction.atomic
def create_pending_booking(*, user, event_id: str, quantity=1, use_points=False) -> Booking:
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

    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        raise ValidationError("Invalid number of tickets.")
    if not 1 <= quantity <= MAX_TICKETS_PER_BOOKING:
        raise ValidationError(
            f"You can book between 1 and {MAX_TICKETS_PER_BOOKING} tickets at a time."
        )

    event = Event.objects.select_for_update().get(pk=event_id)

    if event.status != Event.Status.PUBLISHED:
        raise ValidationError("This event isn't open for booking.")

    if event.spots_left < quantity:
        raise SoldOutError(f"Only {event.spots_left} spot(s) left.")

    already_booked = (
        Booking.objects.filter(event=event, user=user)
        .exclude(status=Booking.Status.CANCELLED)
        .exists()
    )
    if already_booked:
        # user already has a live booking for this event (unique constraint) —
        # surface that instead of silently no-op'ing
        raise ValidationError("You already have a booking for this event.")

    total_minor = ticket_price_minor(event.price_minor) * quantity
    points = 0
    if use_points and total_minor > 0:
        # Lock the user's row so two checkouts at once can't spend the same
        # points twice; the balance is read under that lock.
        from django.contrib.auth import get_user_model
        from loyalty.services import balance
        get_user_model().objects.select_for_update().filter(pk=user.pk).first()
        points = redeemable_points(total_minor, balance(user))

    booking = Booking.objects.create(
        event=event,
        user=user,
        quantity=quantity,
        status=Booking.Status.PENDING,
        price_paid_minor=max(0, total_minor - points * FILS_PER_POINT),
        service_fee_minor=service_fee_minor(event.price_minor) * quantity,
        loyalty_points=loyalty_points(event.price_minor) * quantity,
        points_redeemed=points,
    )
    if points:
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