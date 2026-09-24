"""
bookings app
------------
Owns the act of reserving a spot at an event: who, how many, what status,
and check-in at the door. Depends on accounts + events; payments depends
on this app, not the other way around, so a booking can exist (e.g. for a
free event) with zero payment models involved.
"""

import uuid

from django.conf import settings
from django.db import models


class Booking(models.Model):
    """One user's ticket(s) to one event. QR payload = str(id)."""
    class Status(models.TextChoices):
        PENDING = "pending", "Pending payment"
        CONFIRMED = "confirmed", "Confirmed"
        CANCELLED = "cancelled", "Cancelled"
        ATTENDED = "attended", "Attended"       # set on QR scan at entrance

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(
        "events.Event", on_delete=models.CASCADE, related_name="bookings"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bookings"
    )
    quantity = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    price_paid_minor = models.PositiveIntegerField(default=0)  # snapshot at booking time
    booked_at = models.DateTimeField(auto_now_add=True)
    checked_in_at = models.DateTimeField(blank=True, null=True)  # first person through the door
    # One QR covers the whole booking: a group of `quantity` people can come
    # in together or one at a time, and this tracks how many are in so far.
    checked_in_count = models.PositiveSmallIntegerField(default=0)

    class Meta:
        constraints = [
            # Only one *live* booking per user per event. Cancelled ones are
            # kept as records (abandoned checkouts, refunds) and must not
            # stop the same user from booking again.
            models.UniqueConstraint(
                fields=["event", "user"],
                condition=~models.Q(status="cancelled"),
                name="one_active_booking_per_user_per_event",
            )
        ]

    def __str__(self):
        return f"{self.user} -> {self.event} ({self.status})"