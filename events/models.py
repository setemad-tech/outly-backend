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

# A finished event drops off the public map and Events list this long after
# it ends (or starts, when it has no end time) — long enough for late-night
# events to stay findable while they're still running.
EVENT_LISTED_FOR_HOURS_AFTER = 8


class Category(models.Model):
    """Nightlife, Gaming, Networking, Sports... shown as filter chips."""
    name = models.CharField(max_length=50, unique=True)
    slug = models.SlugField(unique=True)
    icon = models.CharField(max_length=50, blank=True)  # icon key for frontend

    class Meta:
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class EventQuerySet(models.QuerySet):
    def still_listed(self, now=None):
        """Events that haven't finished yet (see EVENT_LISTED_FOR_HOURS_AFTER)."""
        from django.utils import timezone

        cutoff = (now or timezone.now()) - timedelta(hours=EVENT_LISTED_FOR_HOURS_AFTER)
        return self.filter(
            models.Q(end_at__gte=cutoff)
            | models.Q(end_at__isnull=True, start_at__gte=cutoff)
        )


class Event(models.Model):
    objects = EventQuerySet.as_manager()

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

    # Early access (BU-31): members at this loyalty level or above can buy
    # each ticket type this many hours before its sales start. "" = off.
    # Only applies to ticket types that have a sales start time.
    early_access_level = models.CharField(
        max_length=10, blank=True, default="",
        choices=[("explorer", "Explorer"), ("insider", "Insider"), ("icon", "Icon")],
    )
    early_access_hours = models.PositiveSmallIntegerField(default=24)

    # Admission criteria (T&C §5.5: must be shown on the listing and at
    # checkout). null = all ages. entry_restrictions is the organizer's own
    # wording for anything else, e.g. "Women only · Emirates ID required".
    min_age = models.PositiveSmallIntegerField(blank=True, null=True)
    entry_restrictions = models.CharField(max_length=300, blank=True)
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
        # PENDING bookings (someone currently on the Stripe payment page)
        # hold their spot too — otherwise two people could both pay for the
        # last spot. Only recent ones, though: see CHECKOUT_HOLD_MINUTES.
        # Counted in people: a table package booking's quantity is its guests.
        return live_bookings(self.bookings.all()).aggregate(
            total=models.Sum("quantity")
        )["total"] or 0

    @property
    def entry_requirements_display(self):
        """One line for emails/Stripe, e.g. "18+ · Women only" ("" if none)."""
        parts = [f"{self.min_age}+"] if self.min_age else []
        if self.entry_restrictions.strip():
            parts.append(self.entry_restrictions.strip())
        return " · ".join(parts)

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

class TicketType(models.Model):
    """
    One kind of ticket on an event (BU-06/07): General, VIP, "Entry + welcome
    drink", a table for 6... Each saleable unit admits `guests_per_unit`
    people — 1 for an admission ticket, N for a package. The event's
    `capacity` counts people; `quantity` here caps units of this type.

    Sold orders keep their own snapshot (bookings.BookingItem), so editing
    or archiving a type never rewrites tickets already sold.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "On sale"
        PAUSED = "paused", "Paused"
        ARCHIVED = "archived", "Removed"  # had sales, so kept for history

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="ticket_types")
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=300, blank=True)  # inclusions / entry conditions
    price_minor = models.PositiveIntegerField(default=0)  # organizer's price per unit; fee added on top
    guests_per_unit = models.PositiveSmallIntegerField(default=1)
    quantity = models.PositiveIntegerField(blank=True, null=True)  # units for sale; null = event capacity only
    max_per_order = models.PositiveSmallIntegerField(default=10)
    sales_start_at = models.DateTimeField(blank=True, null=True)
    sales_end_at = models.DateTimeField(blank=True, null=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    allow_promo = models.BooleanField(default=True)
    allow_points = models.BooleanField(default=True)
    position = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "created_at"]

    def __str__(self):
        return f"{self.event} · {self.name}"

    @property
    def is_package(self):
        return self.guests_per_unit > 1

    def units_sold(self):
        """Units held by live bookings (paid, or mid-checkout within the hold)."""
        from bookings.models import BookingItem

        return BookingItem.objects.filter(
            ticket_type=self, booking__in=live_bookings(self.event.bookings.all())
        ).aggregate(total=models.Sum("quantity"))["total"] or 0

    def early_access_start_at(self):
        """When members with early access can start buying, or None if the event doesn't offer it."""
        from datetime import timedelta

        event = self.event
        if not (self.sales_start_at and event.early_access_level and event.early_access_hours):
            return None
        return self.sales_start_at - timedelta(hours=event.early_access_hours)

    def sales_start_for(self, level=""):
        """This buyer's sales start: earlier for members with early access."""
        from loyalty.levels import meets

        early = self.early_access_start_at()
        if early is not None and level and meets(level, self.event.early_access_level):
            return early
        return self.sales_start_at

    def sale_state(self, now=None, units_sold=None, spots_left=None, level=""):
        """
        on_sale | paused | not_started | ended | sold_out — what a buyer is
        told about this type. `level` is the buyer's loyalty level (early
        access). Event-level states (cancelled, ended) are handled by the
        event itself.
        """
        from django.utils import timezone

        now = now or timezone.now()
        if self.status != self.Status.ACTIVE:
            return "paused"
        start = self.sales_start_for(level)
        if start and now < start:
            return "not_started"
        if self.sales_end_at and now >= self.sales_end_at:
            return "ended"
        if self.units_left(units_sold, spots_left) <= 0:
            return "sold_out"
        return "on_sale"

    def units_left(self, units_sold=None, spots_left=None):
        """How many more units can be bought: this type's stock and the event's people capacity."""
        spots = self.event.spots_left if spots_left is None else spots_left
        by_capacity = spots // max(self.guests_per_unit, 1)
        if self.quantity is None:
            return by_capacity
        sold = self.units_sold() if units_sold is None else units_sold
        return max(0, min(self.quantity - sold, by_capacity))


def live_bookings(qs):
    """Bookings holding capacity: paid ones, and checkouts started within CHECKOUT_HOLD_MINUTES."""
    from bookings.models import Booking
    from django.conf import settings
    from django.utils import timezone

    hold_cutoff = timezone.now() - timedelta(minutes=settings.CHECKOUT_HOLD_MINUTES)
    return qs.filter(
        models.Q(status__in=[Booking.Status.CONFIRMED, Booking.Status.ATTENDED])
        | models.Q(status=Booking.Status.PENDING, booked_at__gte=hold_cutoff)
    )
