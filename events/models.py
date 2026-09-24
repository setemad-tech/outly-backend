"""
events app
----------
Owns what an event is: where, when, price, capacity, what's included.
Knows about accounts (organizer) but nothing about bookings, payments,
or chat — those apps depend on events, not the reverse. Event exposes
spots_taken/spots_left by querying bookings lazily (import inside the
method) so there's no circular import at module load time.
"""

import uuid
from datetime import timedelta

from django.db import models

# Chat stays open until this many minutes after the event ends (or starts,
# for events with no end_at) — the single source of truth the frontend's
# countdown was previously faking with a hardcoded per-event map.
CHAT_GRACE_MINUTES = 120


class Category(models.Model):
    """Nightlife, Gaming, Networking, Sports... shown as filter chips."""
    name = models.CharField(max_length=50, unique=True)
    slug = models.SlugField(unique=True)
    icon = models.CharField(max_length=50, blank=True)  # icon key for frontend

    class Meta:
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class Event(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        CANCELLED = "cancelled", "Cancelled"
        COMPLETED = "completed", "Completed"

    class Language(models.TextChoices):
        EN = "en", "English"
        RU = "ru", "Russian"
        RU_EN = "ru_en", "Russian / English"
        AR = "ar", "Arabic"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organizer = models.ForeignKey(
        "accounts.OrganizerProfile", on_delete=models.CASCADE, related_name="events"
    )
    category = models.ForeignKey(
        Category, on_delete=models.SET_NULL, null=True, related_name="events"
    )

    title = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True)
    description = models.TextField(blank=True)
    cover_image = models.ImageField(upload_to="events/covers/", blank=True, null=True)

    # location — store coordinates from day one so the map view never needs
    # a migration when you expand past Dubai
    venue_name = models.CharField(max_length=150)          # "Dubai Marina"
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=80, default="Dubai")
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)

    start_at = models.DateTimeField()
    end_at = models.DateTimeField(blank=True, null=True)

    # money as minor units avoids float rounding bugs; 70 AED -> 7000 fils
    price_minor = models.PositiveIntegerField(default=0)   # 0 = free event
    currency = models.CharField(max_length=3, default="AED")

    capacity = models.PositiveIntegerField()
    language = models.CharField(
        max_length=10, choices=Language.choices, default=Language.EN
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.DRAFT
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["start_at"]
        indexes = [
            models.Index(fields=["city", "start_at"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return self.title

    @property
    def spots_taken(self):
        # local import avoids events <-> bookings circular import
        from bookings.models import Booking
        from django.conf import settings
        from django.utils import timezone

        # PENDING bookings (someone currently on the Stripe payment page)
        # hold their spot too — otherwise two people could both pay for the
        # last spot. Only recent ones, though: see CHECKOUT_HOLD_MINUTES.
        hold_cutoff = timezone.now() - timedelta(minutes=settings.CHECKOUT_HOLD_MINUTES)
        return self.bookings.filter(
            models.Q(status__in=[Booking.Status.CONFIRMED, Booking.Status.ATTENDED])
            | models.Q(status=Booking.Status.PENDING, booked_at__gte=hold_cutoff)
        ).aggregate(total=models.Sum("quantity"))["total"] or 0

    @property
    def spots_left(self):
        return max(self.capacity - self.spots_taken, 0)

    @property
    def chat_closes_at(self):
        anchor = self.end_at or self.start_at  # fall back to start_at if no end_at set
        return anchor + timedelta(minutes=CHAT_GRACE_MINUTES)

    @property
    def chat_is_open(self):
        from django.utils import timezone
        return timezone.now() < self.chat_closes_at


class EventPerk(models.Model):
    """A single "what's included" line item, e.g. '1 welcome drink'."""
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="perks")
    label = models.CharField(max_length=100)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return self.label