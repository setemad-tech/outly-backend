"""
payments app
------------
Owns money moving. Deliberately its own app, decoupled from bookings, so:
- free events never touch this app at all
- adding a second provider, refunds, or payouts to organizers later is a
  change contained entirely to this app
- if you ever need PCI-adjacent handling/auditing, it's already isolated
"""

from django.db import models


class Payment(models.Model):
    class Provider(models.TextChoices):
        STRIPE = "stripe", "Stripe"
        MANUAL = "manual", "Manual / cash"

    booking = models.OneToOneField(
        "bookings.Booking", on_delete=models.CASCADE, related_name="payment"
    )
    provider = models.CharField(max_length=20, choices=Provider.choices)
    provider_reference = models.CharField(max_length=120, blank=True)
    amount_minor = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default="AED")
    paid_at = models.DateTimeField(blank=True, null=True)
    refunded_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"{self.amount_minor / 100} {self.currency} — {self.provider}"