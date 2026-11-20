from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce
from rest_framework import serializers

from accounts.models import OrganizerProfile
from events.models import Event, TicketType
from .models import PromoCode, normalize_code


class PromoCodeSerializer(serializers.ModelSerializer):
    """
    Only OUTLY staff create codes. A code can be OUTLY-wide, for one
    organizer's events, or for one event (and optionally some of its ticket
    types), and staff choose who pays the discount: OUTLY, or the organizer
    out of their share. Organizers only see the codes on their events.
    Once created, only the switch, limits and end date can change — never
    the discount itself — so the terms past orders were sold under stay
    true (BU-30).
    """
    organizer = serializers.PrimaryKeyRelatedField(
        queryset=OrganizerProfile.objects.filter(status=OrganizerProfile.Status.APPROVED),
        required=False, allow_null=True,
    )
    event_title = serializers.CharField(source="event.title", read_only=True, allow_null=True)
    organizer_name = serializers.CharField(source="organizer.display_name", read_only=True, allow_null=True)
    ticket_types = serializers.PrimaryKeyRelatedField(
        many=True, queryset=TicketType.objects.all(), required=False
    )
    ticket_type_names = serializers.SerializerMethodField()
    uses = serializers.IntegerField(read_only=True)
    paid_uses = serializers.IntegerField(read_only=True)
    discount_given_minor = serializers.IntegerField(read_only=True)
    label = serializers.CharField(source="describe", read_only=True)

    class Meta:
        model = PromoCode
        fields = [
            "id", "code", "kind", "value", "label", "max_discount_minor", "min_subtotal_minor",
            "event", "event_title", "organizer", "organizer_name", "ticket_types", "ticket_type_names",
            "funded_by", "min_level", "starts_at", "ends_at", "max_redemptions", "per_user_limit", "is_active",
            "uses", "paid_uses", "discount_given_minor", "created_at",
        ]
        read_only_fields = ["created_at"]

    EDITABLE_LATER = {"is_active", "ends_at", "max_redemptions", "per_user_limit"}

    def get_ticket_type_names(self, promo):
        return [t.name for t in promo.ticket_types.all()]

    def validate_code(self, v):
        code = normalize_code(v)
        if not 3 <= len(code) <= 40 or not code.replace("-", "").replace("_", "").isalnum():
            raise serializers.ValidationError("Use 3-40 letters, numbers, - or _.")
        qs = PromoCode.objects.filter(code=code)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("That code is already taken.")
        return code

    def validate(self, data):
        if self.instance is not None:
            changed = set(data) - self.EDITABLE_LATER
            if changed:
                raise serializers.ValidationError(
                    "Only the on/off switch, end date and limits can change after a code is made."
                )
            return data

        kind, value = data.get("kind"), data.get("value") or 0
        if kind == PromoCode.Kind.PERCENT and not 1 <= value <= 100:
            raise serializers.ValidationError({"value": "Percentage must be 1-100."})
        if kind == PromoCode.Kind.FIXED and (value < 100 or value % 100):
            raise serializers.ValidationError({"value": "Use a whole AED amount, at least 1 AED."})
        for f in ("max_discount_minor", "min_subtotal_minor"):
            if data.get(f) is not None and data[f] % 100:
                raise serializers.ValidationError({f: "Use whole AED amounts."})
        start, end = data.get("starts_at"), data.get("ends_at")
        if start and end and end <= start:
            raise serializers.ValidationError({"ends_at": "The code must end after it starts."})
        if data.get("per_user_limit", 1) < 1:
            raise serializers.ValidationError({"per_user_limit": "At least 1."})

        event = data.get("event")
        organizer = data.get("organizer")
        if event is not None:
            if organizer is not None and event.organizer_id != organizer.id:
                raise serializers.ValidationError({"event": "That event belongs to another organizer."})
            data["organizer"] = organizer = event.organizer
        if data.get("funded_by") == PromoCode.FundedBy.ORGANIZER and organizer is None:
            raise serializers.ValidationError(
                {"funded_by": "Pick the organizer or event — an organizer can only pay for codes on their own events."}
            )
        types = data.get("ticket_types") or []
        if types:
            if event is None:
                raise serializers.ValidationError({"ticket_types": "Pick the event first."})
            if any(t.event_id != event.id for t in types):
                raise serializers.ValidationError({"ticket_types": "Those tickets aren't on this event."})
        return data

    def create(self, validated_data):
        validated_data.setdefault("funded_by", PromoCode.FundedBy.OUTLY)
        validated_data["created_by"] = self.context["request"].user
        return super().create(validated_data)


def with_usage(qs):
    """Annotates uses (live orders), paid_uses and discount_given_minor (paid orders only)."""
    from datetime import timedelta
    from django.conf import settings
    from django.utils import timezone
    from bookings.models import Booking

    sold = Q(bookings__status__in=[Booking.Status.CONFIRMED, Booking.Status.ATTENDED])
    held = Q(
        bookings__status=Booking.Status.PENDING,
        bookings__booked_at__gte=timezone.now() - timedelta(minutes=settings.CHECKOUT_HOLD_MINUTES),
    )
    return qs.select_related("event", "organizer").prefetch_related("ticket_types").annotate(
        uses=Count("bookings", filter=sold | held, distinct=True),
        paid_uses=Count("bookings", filter=sold, distinct=True),
        discount_given_minor=Coalesce(Sum("bookings__discount_minor", filter=sold), 0),
    )
