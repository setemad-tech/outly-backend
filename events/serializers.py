from rest_framework import serializers
from .models import Event, EventPerk


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

    class Meta:
        model = Event
        fields = [
            "id", "title", "slug", "description", "cover_image",
            "venue_name", "city", "latitude", "longitude",
            "start_at", "end_at", "price_minor", "currency",
            "capacity", "spots_left", "language", "status",
            "category", "category_name", "organizer", "organizer_name", "perks",
            "chat_closes_at",
        ]
        read_only_fields = ["id", "organizer", "spots_left"]

    def create(self, validated_data):
        perks_data = validated_data.pop("perks", [])
        # organizer comes from the request, never from client-supplied data —
        # otherwise anyone could create an event "on behalf of" someone else
        validated_data["organizer"] = self.context["request"].user.organizer_profile
        event = Event.objects.create(**validated_data)
        EventPerk.objects.bulk_create(
            [EventPerk(event=event, **p) for p in perks_data]
        )
        return event