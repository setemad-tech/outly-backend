"""
loyalty app
-----------
Loyalty points as a ledger: every change to someone's points is one
LoyaltyEntry row (+ earned, - reversed, - redeemed at checkout, + returned
when that checkout is abandoned, +/- staff adjustment), and a balance
is just the sum. Nothing is ever edited in place, so the history always
explains the balance.

How many points a ticket earns lives in events/pricing.py.
"""

from django.conf import settings
from django.db import models


class LoyaltyEntry(models.Model):
    class Kind(models.TextChoices):
        EARNED = "earned", "Earned from a booking"
        REVERSED = "reversed", "Booking cancelled"
        REDEEMED = "redeemed", "Used at checkout"
        RETURNED = "returned", "Returned — checkout not completed"
        ADJUSTED = "adjusted", "Adjusted by OUTLY"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="loyalty_entries"
    )
    points = models.IntegerField()  # signed
    kind = models.CharField(max_length=10, choices=Kind.choices)
    # SET_NULL: if an admin deletes the event (and with it the booking),
    # the attendee keeps the points they earned.
    booking = models.ForeignKey(
        "bookings.Booking", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="loyalty_entries",
    )
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "loyalty entries"
        constraints = [
            # Per booking, each kind happens at most once — Stripe webhook
            # redeliveries can't double-award, and a redemption can't be
            # returned twice.
            models.UniqueConstraint(
                fields=["booking", "kind"],
                condition=models.Q(booking__isnull=False),
                name="one_loyalty_entry_per_booking_and_kind",
            )
        ]

    def __str__(self):
        return f"{self.user} {self.points:+d} ({self.kind})"
