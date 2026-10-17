from rest_framework import serializers

from .models import LoyaltyEntry


class LoyaltyEntrySerializer(serializers.ModelSerializer):
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)
    event_id = serializers.UUIDField(source="booking.event_id", read_only=True, allow_null=True)

    class Meta:
        model = LoyaltyEntry
        fields = ["id", "points", "kind", "kind_label", "note", "event_id", "created_at"]
