from django.utils.text import slugify
from rest_framework import serializers
from .models import Category, Event, EventPerk


class EventPerkSerializer(serializers.ModelSerializer):
    class Meta:
        model = EventPerk
        fields = ["label", "order"]


class EventSerializer(serializers.ModelSerializer):
    perks = EventPerkSerializer(many=True, required=False)
    organizer_name = serializers.CharField(source="organizer.display_name", read_only=True)
    category_name = serializers.CharField(source="category.name", read_only=True, allow_null=True)
    spots_left = serializers.IntegerField(read_only=True)
    chat_closes_at = serializers.DateTimeField(read_only=True)
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
            "start_at", "end_at", "price_minor", "currency",
            "capacity", "spots_left", "min_age", "entry_restrictions", "language", "status",
            "category", "category_name", "category_label", "organizer", "organizer_name", "perks",
            "chat_closes_at",
        ]
        read_only_fields = ["id", "organizer", "spots_left"]

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

    def update(self, instance, validated_data):
        self._apply_category_label(validated_data)
        validated_data.pop("perks", None)  # perks aren't editable through PATCH yet
        return super().update(instance, validated_data)

    def create(self, validated_data):
        self._apply_category_label(validated_data)
        perks_data = validated_data.pop("perks", [])
        # organizer comes from the request, never from client-supplied data —
        # otherwise anyone could create an event "on behalf of" someone else
        validated_data["organizer"] = self.context["request"].user.organizer_profile
        event = Event.objects.create(**validated_data)
        EventPerk.objects.bulk_create(
            [EventPerk(event=event, **p) for p in perks_data]
        )
        return event