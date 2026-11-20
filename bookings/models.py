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
    # People this booking admits (sum over its items of units x guests per
    # unit) — what event capacity and check-in count. The line-by-line
    # breakdown is in BookingItem.
    quantity = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    # Snapshots at booking time (events/pricing.py), so later price or
    # percentage changes never rewrite history:
    price_paid_minor = models.PositiveIntegerField(default=0)   # what the attendee paid, fee included
    service_fee_minor = models.PositiveIntegerField(default=0)  # OUTLY's part of that; the rest is the organizer's
    loyalty_points = models.PositiveIntegerField(default=0)     # points this booking earns once paid
    points_redeemed = models.PositiveIntegerField(default=0)    # points spent on it (1 point = 1 AED off price_paid_minor)
    # Promo code (promos app). discount_minor came off the price before
    # points; outly_discount_minor is the part OUTLY funds (platform codes),
    # so it isn't taken out of the organizer's share. Code text is a
    # snapshot in case the code is later deleted.
    promo_code = models.ForeignKey(
        "promos.PromoCode", on_delete=models.SET_NULL, null=True, blank=True, related_name="bookings"
    )
    promo_code_text = models.CharField(max_length=40, blank=True)
    discount_minor = models.PositiveIntegerField(default=0)
    outly_discount_minor = models.PositiveIntegerField(default=0)
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


class BookingItem(models.Model):
    """
    One ticket type within a booking: "2 x VIP" or "1 x Table for 6".
    Everything is copied from the TicketType at the moment of booking, so a
    later price change, rename or removal never rewrites what was sold.
    """
    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="items")
    ticket_type = models.ForeignKey(
        "events.TicketType", on_delete=models.SET_NULL, null=True, blank=True, related_name="items"
    )
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=300, blank=True)  # "includes a welcome drink"
    quantity = models.PositiveSmallIntegerField()          # units (tickets or packages)
    guests_per_unit = models.PositiveSmallIntegerField(default=1)
    unit_base_minor = models.PositiveIntegerField()        # organizer's price per unit
    unit_price_minor = models.PositiveIntegerField()       # attendee price per unit, fee included
    service_fee_minor = models.PositiveIntegerField(default=0)  # whole line
    discount_minor = models.PositiveIntegerField(default=0)     # promo share of this line
    loyalty_points = models.PositiveIntegerField(default=0)     # whole line

    class Meta:
        ordering = ["id"]

    @property
    def guests(self):
        return self.quantity * self.guests_per_unit

    @property
    def line_total_minor(self):
        return self.unit_price_minor * self.quantity

    def __str__(self):
        return f"{self.quantity} x {self.name}"
