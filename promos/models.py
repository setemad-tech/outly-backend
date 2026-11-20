"""
promos app
----------
Promo codes (BU-30): a percentage or fixed AED amount off tickets.

Only OUTLY staff create codes (admin portal). A code covers every event,
one organizer's events (organizer set) or one event (event set, organizer
copied from it). Staff choose who pays for it:
  - funded_by=outly: OUTLY absorbs the discount, the organizer's proceeds
    don't change;
  - funded_by=organizer: it comes out of the organizer's share (only for
    codes tied to that organizer). Organizers can see these codes, not edit.

A code is "used" by every live booking that carries it (paid, or mid-
checkout within the hold — bookings.Booking.promo_code). A cancelled or
abandoned booking stops counting automatically, so an expired checkout
never eats a limited redemption, and a refund gives the use back.

The maths is in promos/services.py; nothing here is ever edited once a
booking uses it except the on/off switch, limits and end date.
"""

from django.conf import settings
from django.db import models


class PromoCode(models.Model):
    class Kind(models.TextChoices):
        PERCENT = "percent", "Percentage off"
        FIXED = "fixed", "Fixed amount off"

    class FundedBy(models.TextChoices):
        ORGANIZER = "organizer", "Organizer"
        OUTLY = "outly", "OUTLY"

    # Stored upper-case, so "summer10" and "SUMMER10" are the same code.
    code = models.CharField(max_length=40, unique=True)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    # percent: 1-100. fixed: fils off the whole order (whole AED).
    value = models.PositiveIntegerField()
    max_discount_minor = models.PositiveIntegerField(blank=True, null=True)
    min_subtotal_minor = models.PositiveIntegerField(blank=True, null=True)

    # Scope. organizer=None means an OUTLY code. event=None means all of
    # the organizer's events (or every event, for an OUTLY code).
    organizer = models.ForeignKey(
        "accounts.OrganizerProfile", on_delete=models.CASCADE, null=True, blank=True,
        related_name="promo_codes",
    )
    event = models.ForeignKey(
        "events.Event", on_delete=models.CASCADE, null=True, blank=True, related_name="promo_codes"
    )
    # Empty = every ticket type that allows promo codes.
    ticket_types = models.ManyToManyField("events.TicketType", blank=True, related_name="promo_codes")
    # Members-only (BU-31): only buyers at this loyalty level or above. "" = everyone.
    min_level = models.CharField(
        max_length=10, blank=True, default="",
        choices=[("explorer", "Explorer"), ("insider", "Insider"), ("icon", "Icon")],
    )
    funded_by = models.CharField(max_length=10, choices=FundedBy.choices)

    starts_at = models.DateTimeField(blank=True, null=True)
    ends_at = models.DateTimeField(blank=True, null=True)
    max_redemptions = models.PositiveIntegerField(blank=True, null=True)  # orders, all users
    per_user_limit = models.PositiveSmallIntegerField(default=1)
    is_active = models.BooleanField(default=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.code

    def save(self, *args, **kwargs):
        self.code = normalize_code(self.code)
        super().save(*args, **kwargs)

    def describe(self) -> str:
        if self.kind == self.Kind.PERCENT:
            return f"{self.value}% off"
        return f"{self.value // 100} AED off"


def normalize_code(raw) -> str:
    return "".join(str(raw or "").split()).upper()[:40]
