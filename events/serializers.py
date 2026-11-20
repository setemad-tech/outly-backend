import json

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import serializers
from .models import Category, Event, EventPerk, TicketType
from .pricing import fee_percent_for, loyalty_points, ticket_price_minor

# Smallest paid price an organizer can set (1 AED -> 2 AED with the fee,
# Stripe's minimum card charge in AED).
MIN_PAID_PRICE_MINOR = 100
MAX_TICKET_TYPES = 10
MAX_GUESTS_PER_UNIT = 50


class TicketTypeSerializer(serializers.ModelSerializer):
    """
    What buyers see of a ticket type. The organizer's own views add stock
    and sales (see EventSerializer.get_ticket_types). `units_sold` comes
    from the prefetch in EventViewSet (annotated), else is counted.
    """
    ticket_price_minor = serializers.SerializerMethodField()
    loyalty_points = serializers.SerializerMethodField()
    sale_state = serializers.SerializerMethodField()
    units_left = serializers.SerializerMethodField()
    early_access_start_at = serializers.SerializerMethodField()
    early_access_for_you = serializers.SerializerMethodField()
    early_access_level = serializers.CharField(source="event.early_access_level", read_only=True)

    class Meta:
        model = TicketType
        fields = [
            "id", "name", "description", "price_minor", "ticket_price_minor", "loyalty_points",
            "guests_per_unit", "max_per_order", "sales_start_at", "sales_end_at",
            "sale_state", "units_left", "allow_promo", "allow_points", "position",
            "early_access_start_at", "early_access_for_you", "early_access_level",
        ]

    def _sold(self, tt):
        sold = getattr(tt, "sold", None)
        return tt.units_sold() if sold is None else sold

    def _spots(self, tt):
        cache = self.context.setdefault("_spots_left", {})
        if tt.event_id not in cache:
            cache[tt.event_id] = tt.event.spots_left
        return cache[tt.event_id]

    def get_ticket_price_minor(self, tt):
        return ticket_price_minor(tt.price_minor, fee_percent_for(tt.event))

    def get_loyalty_points(self, tt):
        from loyalty.levels import points_percent
        return loyalty_points(tt.price_minor, points_percent(self._level()))

    def _level(self):
        """The viewer's loyalty level, worked out once per response."""
        if "_viewer_level" not in self.context:
            from loyalty.levels import user_level
            request = self.context.get("request")
            self.context["_viewer_level"] = user_level(getattr(request, "user", None))
        return self.context["_viewer_level"]

    def get_sale_state(self, tt):
        return tt.sale_state(units_sold=self._sold(tt), spots_left=self._spots(tt), level=self._level())

    def get_early_access_start_at(self, tt):
        early = tt.early_access_start_at()
        return early.isoformat() if early else None

    def get_early_access_for_you(self, tt):
        """On sale for this viewer only because of their level (the public sale hasn't started)."""
        from django.utils import timezone

        level = self._level()
        if not level or tt.sales_start_at is None or timezone.now() >= tt.sales_start_at:
            return False
        return self.get_sale_state(tt) == "on_sale"

    def get_units_left(self, tt):
        return tt.units_left(units_sold=self._sold(tt), spots_left=self._spots(tt))


class OrganizerTicketTypeSerializer(TicketTypeSerializer):
    units_sold = serializers.SerializerMethodField()

    class Meta(TicketTypeSerializer.Meta):
        fields = TicketTypeSerializer.Meta.fields + ["status", "quantity", "units_sold"]

    def get_units_sold(self, tt):
        return self._sold(tt)


class TicketTypeInput(serializers.Serializer):
    """One ticket type as the organizer's event form sends it."""
    id = serializers.UUIDField(required=False, allow_null=True)
    name = serializers.CharField(max_length=80)
    description = serializers.CharField(max_length=300, required=False, allow_blank=True, default="")
    price_minor = serializers.IntegerField(min_value=0, max_value=10_000_000)
    guests_per_unit = serializers.IntegerField(min_value=1, max_value=MAX_GUESTS_PER_UNIT, default=1)
    quantity = serializers.IntegerField(min_value=1, max_value=100_000, required=False, allow_null=True, default=None)
    max_per_order = serializers.IntegerField(min_value=1, max_value=10, default=10)
    sales_start_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    sales_end_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    status = serializers.ChoiceField(
        choices=[TicketType.Status.ACTIVE, TicketType.Status.PAUSED], default=TicketType.Status.ACTIVE
    )
    allow_promo = serializers.BooleanField(default=True)
    allow_points = serializers.BooleanField(default=True)

    def validate_name(self, v):
        v = v.strip()
        if not v:
            raise serializers.ValidationError("Give the ticket a name.")
        return v

    def validate(self, data):
        price = data["price_minor"]
        if 0 < price < MIN_PAID_PRICE_MINOR:
            raise serializers.ValidationError({"price_minor": "A paid ticket must cost at least 1 AED."})
        if price % 100:
            raise serializers.ValidationError({"price_minor": "Use whole AED amounts."})
        start, end = data.get("sales_start_at"), data.get("sales_end_at")
        if start and end and end <= start:
            raise serializers.ValidationError({"sales_end_at": "Sales must end after they start."})
        return data


class TicketTypesField(serializers.Field):
    """A list of ticket types — as JSON, or a JSON string (multipart uploads with a cover image)."""

    def to_internal_value(self, data):
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                raise serializers.ValidationError("Invalid ticket types.")
        if not isinstance(data, list):
            raise serializers.ValidationError("Invalid ticket types.")
        if not 1 <= len(data) <= MAX_TICKET_TYPES:
            raise serializers.ValidationError(f"An event needs between 1 and {MAX_TICKET_TYPES} ticket types.")
        rows = TicketTypeInput(data=data, many=True)
        if not rows.is_valid():
            first = next((e for e in rows.errors if e), {})
            msg = next(iter(first.values()))[0] if first else "Invalid ticket types."
            raise serializers.ValidationError(str(msg))
        return rows.validated_data

    def to_representation(self, value):
        return value


class EventPerkSerializer(serializers.ModelSerializer):
    class Meta:
        model = EventPerk
        fields = ["label", "order"]


class EventSerializer(serializers.ModelSerializer):
    perks = EventPerkSerializer(many=True, required=False)
    ticket_types = serializers.SerializerMethodField()
    from_price_minor = serializers.SerializerMethodField()
    organizer_name = serializers.CharField(source="organizer.display_name", read_only=True)
    organizer_logo = serializers.ImageField(source="organizer.logo", read_only=True)
    organizer_verified = serializers.BooleanField(source="organizer.is_verified", read_only=True)
    category_name = serializers.CharField(source="category.name", read_only=True, allow_null=True)
    spots_left = serializers.IntegerField(read_only=True)
    chat_closes_at = serializers.DateTimeField(read_only=True)
    # price_minor is the organizer's price; attendees see and pay this one.
    ticket_price_minor = serializers.SerializerMethodField()
    service_fee_percent = serializers.SerializerMethodField()
    loyalty_points = serializers.SerializerMethodField()
    min_age = serializers.IntegerField(
        required=False, allow_null=True, min_value=1, max_value=99
    )
    # The organizer's event form picks a category by NAME (the same list the
    # filter bar uses) — matched to a Category row, created on first use.
    # Blank clears it.
    category_label = serializers.CharField(
        write_only=True, required=False, allow_blank=True, max_length=50
    )

    class Meta:
        model = Event
        fields = [
            "id", "title", "slug", "description", "cover_image",
            "venue_name", "city", "latitude", "longitude",
            "start_at", "end_at", "price_minor", "ticket_price_minor", "from_price_minor", "service_fee_percent",
            "loyalty_points", "currency",
            "capacity", "spots_left", "min_age", "entry_restrictions", "language", "status",
            "category", "category_name", "category_label", "organizer", "organizer_name", "organizer_logo", "organizer_verified", "perks",
            "chat_closes_at", "ticket_types", "early_access_level", "early_access_hours",
        ]
        read_only_fields = ["id", "organizer", "spots_left"]

    def to_internal_value(self, data):
        # ticket_types is read as a computed list but written as the
        # organizer's full list of types (see _sync_ticket_types).
        raw = data.get("ticket_types") if hasattr(data, "get") else None
        validated = super().to_internal_value(data)
        if raw is not None:
            try:
                validated["ticket_types"] = TicketTypesField().to_internal_value(raw)
            except serializers.ValidationError as e:
                raise serializers.ValidationError({"ticket_types": e.detail})
        return validated

    def _types_for_display(self, event):
        prefetched = getattr(event, "_prefetched_objects_cache", {}).get("ticket_types")
        types = list(prefetched) if prefetched is not None else list(event.ticket_types.all())
        return [t for t in types if t.status != TicketType.Status.ARCHIVED]

    def _is_owner(self, event):
        request = self.context.get("request")
        profile = getattr(getattr(request, "user", None), "organizer_profile", None)
        return profile is not None and profile.id == event.organizer_id

    def get_ticket_types(self, event):
        types = self._types_for_display(event)
        if self._is_owner(event):
            return OrganizerTicketTypeSerializer(types, many=True, context=self.context).data
        public = [t for t in types if t.status == TicketType.Status.ACTIVE]
        return TicketTypeSerializer(public, many=True, context=self.context).data

    def get_from_price_minor(self, event):
        """
        Lowest attendee price you can buy right now (a 'From' price). Falls
        back to the cheapest listed type when nothing is on sale.
        """
        types = [t for t in self._types_for_display(event) if t.status == TicketType.Status.ACTIVE]
        fee = fee_percent_for(event)
        if not types:
            return ticket_price_minor(event.price_minor, fee)
        ser = TicketTypeSerializer(context=self.context)
        on_sale = [t for t in types if ser.get_sale_state(t) == "on_sale"]
        return min(ticket_price_minor(t.price_minor, fee) for t in (on_sale or types))

    def get_ticket_price_minor(self, event):
        return ticket_price_minor(event.price_minor, fee_percent_for(event))

    def get_service_fee_percent(self, event):
        return fee_percent_for(event)

    def get_loyalty_points(self, event):
        # What this viewer would earn (level 0 earns none).
        from loyalty.levels import points_percent
        return loyalty_points(event.price_minor, points_percent(TicketTypeSerializer(context=self.context)._level()))

    def _apply_category_label(self, validated_data):
        if "category_label" not in validated_data:
            return
        name = validated_data.pop("category_label").strip()
        if not name:
            validated_data["category"] = None
            return
        category = Category.objects.filter(name__iexact=name).first()
        if category is None:
            category = Category.objects.create(name=name, slug=slugify(name) or "category")
        validated_data["category"] = category

    def validate(self, data):
        types = data.get("ticket_types")
        capacity = data.get("capacity", getattr(self.instance, "capacity", None))
        if types and capacity:
            too_big = [t["name"] for t in types if t["guests_per_unit"] > capacity]
            if too_big:
                raise serializers.ValidationError(
                    {"ticket_types": f"{too_big[0]} admits more guests than the event's capacity."}
                )
        hours = data.get("early_access_hours")
        if hours is not None and not 1 <= hours <= 168:
            raise serializers.ValidationError({"early_access_hours": "Early access can open 1 to 168 hours (7 days) early."})
        return data

    @transaction.atomic
    def update(self, instance, validated_data):
        self._apply_category_label(validated_data)
        validated_data.pop("perks", None)  # perks aren't editable through PATCH yet
        types = validated_data.pop("ticket_types", None)
        event = super().update(instance, validated_data)
        if types is not None:
            _sync_ticket_types(event, types)
        elif "price_minor" in validated_data and event.ticket_types.count() == 1:
            # Old single-price form: keep its one ticket type in step.
            event.ticket_types.update(price_minor=event.price_minor)
        return event

    @transaction.atomic
    def create(self, validated_data):
        self._apply_category_label(validated_data)
        perks_data = validated_data.pop("perks", [])
        types = validated_data.pop("ticket_types", None)
        # organizer comes from the request, never from client-supplied data —
        # otherwise anyone could create an event "on behalf of" someone else
        validated_data["organizer"] = self.context["request"].user.organizer_profile
        event = Event.objects.create(**validated_data)
        EventPerk.objects.bulk_create(
            [EventPerk(event=event, **p) for p in perks_data]
        )
        if not types:
            types = [{
                "name": "General admission", "description": "", "price_minor": event.price_minor,
                "guests_per_unit": 1, "quantity": None, "max_per_order": 10,
                "sales_start_at": None, "sales_end_at": None, "status": TicketType.Status.ACTIVE,
                "allow_promo": True, "allow_points": True,
            }]
        _sync_ticket_types(event, types)
        return event


def _sync_ticket_types(event, rows):
    """
    Makes the event's ticket types match the organizer's list, in that order.
    Types with sales are never deleted (archived instead) and never lose
    their history: what was sold lives on in bookings.BookingItem. A price
    change only affects tickets sold from now on.
    """
    existing = {str(t.id): t for t in event.ticket_types.all()}
    keep = set()
    fields = [
        "name", "description", "price_minor", "guests_per_unit", "quantity", "max_per_order",
        "sales_start_at", "sales_end_at", "status", "allow_promo", "allow_points",
    ]
    for position, row in enumerate(rows):
        tt = existing.get(str(row.get("id"))) if row.get("id") else None
        if tt is None:
            tt = TicketType(event=event)
        elif tt.items.exists() and row["guests_per_unit"] != tt.guests_per_unit:
            # People already hold tickets admitting the old number of guests.
            raise serializers.ValidationError(
                {"ticket_types": f"{tt.name} has sales, so its guests per ticket can't change. Add a new ticket type instead."}
            )
        for f in fields:
            setattr(tt, f, row.get(f))
        tt.position = position
        tt.save()
        keep.add(str(tt.id))

    for key, tt in existing.items():
        if key in keep or tt.status == TicketType.Status.ARCHIVED:
            continue
        if tt.items.exists():
            tt.status = TicketType.Status.ARCHIVED
            tt.save(update_fields=["status"])
        else:
            tt.delete()

    # Event.price_minor stays the cheapest type on sale, for anything that
    # still reads a single price (old app versions, emails, analytics).
    listed = event.ticket_types.exclude(status=TicketType.Status.ARCHIVED)
    prices = (
        list(listed.filter(status=TicketType.Status.ACTIVE).values_list("price_minor", flat=True))
        or list(listed.values_list("price_minor", flat=True))
        or [0]
    )
    event.price_minor = min(prices)
    event.save(update_fields=["price_minor"])