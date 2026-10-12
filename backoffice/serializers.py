from django.contrib.auth import get_user_model
from django.db.models import Sum
from rest_framework import serializers

from accounts.models import OrganizerProfile
from bookings.models import Booking
from events.models import Event

from .models import AdminAction

User = get_user_model()


class AdminOrganizerSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)
    user_name = serializers.CharField(source="user.get_full_name", read_only=True)
    user_id = serializers.IntegerField(source="user.id", read_only=True)
    reviewed_by = serializers.EmailField(source="reviewed_by.email", read_only=True, default=None)
    events_count = serializers.IntegerField(read_only=True)
    is_verified = serializers.BooleanField(read_only=True)

    class Meta:
        model = OrganizerProfile
        fields = [
            "id", "display_name", "logo", "bio", "city", "instagram", "website", "event_types",
            "status", "is_verified", "review_note", "reviewed_at", "reviewed_by", "created_at",
            "email", "user_name", "user_id", "stripe_onboarding_complete", "events_count",
        ]


class AdminEventSerializer(serializers.ModelSerializer):
    organizer_name = serializers.CharField(source="organizer.display_name", read_only=True)
    organizer_verified = serializers.BooleanField(source="organizer.is_verified", read_only=True)
    category_name = serializers.CharField(source="category.name", read_only=True, default=None)
    tickets_sold = serializers.IntegerField(read_only=True)
    revenue_minor = serializers.IntegerField(read_only=True)

    class Meta:
        model = Event
        fields = [
            "id", "title", "cover_image", "venue_name", "city", "start_at", "end_at", "status",
            "price_minor", "currency", "capacity", "category_name", "organizer", "organizer_name",
            "organizer_verified", "tickets_sold", "revenue_minor", "created_at",
        ]


class AdminUserSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="get_full_name", read_only=True)
    organizer_status = serializers.CharField(source="organizer_profile.status", read_only=True, default=None)
    bookings_count = serializers.IntegerField(read_only=True)
    spent_minor = serializers.IntegerField(read_only=True)

    class Meta:
        model = User
        fields = [
            "id", "name", "first_name", "last_name", "email", "gender", "city", "phone_number",
            "email_verified", "is_active", "is_staff", "is_superuser",
            "date_joined", "last_login", "organizer_status", "bookings_count", "spent_minor",
        ]


class AdminUserDetailSerializer(AdminUserSerializer):
    """One account, plus what deleting it would take with it."""
    organizer_name = serializers.CharField(source="organizer_profile.display_name", read_only=True, default=None)
    organizer_events = serializers.SerializerMethodField()
    bookings_total = serializers.SerializerMethodField()

    class Meta(AdminUserSerializer.Meta):
        fields = AdminUserSerializer.Meta.fields + ["organizer_name", "organizer_events", "bookings_total"]

    def get_organizer_events(self, user):
        profile = getattr(user, "organizer_profile", None)
        return profile.events.count() if profile else 0

    def get_bookings_total(self, user):
        return user.bookings.count()


class AdminUserUpdateSerializer(serializers.ModelSerializer):
    """What staff may change on an account from the admin portal."""

    class Meta:
        model = User
        fields = [
            "first_name", "last_name", "email", "gender", "city", "phone_number",
            "email_verified", "is_active", "is_staff",
        ]
        extra_kwargs = {"email": {"validators": []}}  # case-insensitive check below instead

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email__iexact=value).exclude(pk=self.instance.pk).exists():
            raise serializers.ValidationError("Another account already uses this email.")
        return value


class AdminBookingSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True)
    user_name = serializers.CharField(source="user.get_full_name", read_only=True)
    event_id = serializers.UUIDField(source="event.id", read_only=True)
    event_title = serializers.CharField(source="event.title", read_only=True)
    event_start_at = serializers.DateTimeField(source="event.start_at", read_only=True)
    currency = serializers.CharField(source="event.currency", read_only=True)
    payment_provider = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            "id", "status", "quantity", "checked_in_count", "price_paid_minor", "currency",
            "booked_at", "checked_in_at", "user_email", "user_name", "event_id", "event_title",
            "event_start_at", "payment_provider",
        ]

    def get_payment_provider(self, booking):
        payment = getattr(booking, "payment", None)
        return payment.provider if payment else None


class AdminActionSerializer(serializers.ModelSerializer):
    actor_email = serializers.EmailField(source="actor.email", read_only=True, default=None)

    class Meta:
        model = AdminAction
        fields = ["id", "actor_email", "action", "target_type", "target_id", "target_label", "details", "created_at"]
