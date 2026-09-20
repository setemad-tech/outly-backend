"""
accounts/serializers.py
-------------------------
All serializers for the accounts app. views.py imports from here — nothing
in this file should ever import from views.py (that direction only goes
one way).
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from .models import OrganizerProfile

User = get_user_model()


class OrganizerProfileSerializer(serializers.ModelSerializer):
    # Override the auto-generated field — DRF's default for DecimalField
    # renders as a STRING ("0.00") to avoid float precision loss, which is
    # right for money but wrong here: the frontend calls .toFixed() on
    # this expecting a real number. coerce_to_string=False makes it come
    # through as an actual JSON number (0.0) instead.
    rating = serializers.DecimalField(max_digits=3, decimal_places=2, coerce_to_string=False)

    class Meta:
        model = OrganizerProfile
        fields = ["display_name", "bio", "is_verified", "rating", "events_hosted"]


class ProfileSerializer(serializers.ModelSerializer):
    # frontend's AppUser type is { id, name, email } — expose `name` as a
    # computed field rather than making the frontend stitch together
    # first_name/last_name itself
    name = serializers.CharField(source="get_full_name", read_only=True)
    organizer = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "name", "email", "phone_number", "city",
            "preferred_language", "organizer", "email_verified",
        ]
        read_only_fields = ["id", "email_verified"]

    def get_organizer(self, user):
        profile = getattr(user, "organizer_profile", None)
        return OrganizerProfileSerializer(profile).data if profile else None


class RegisterSerializer(serializers.ModelSerializer):
    name = serializers.CharField(write_only=True)
    password = serializers.CharField(write_only=True, validators=[validate_password])

    class Meta:
        model = User
        fields = ["name", "email", "password"]

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def create(self, validated_data):
        name = validated_data.pop("name", "")
        first_name, _, last_name = name.partition(" ")
        user = User(
            email=validated_data["email"],
            first_name=first_name,
            last_name=last_name,
        )
        user.set_password(validated_data["password"])
        user.save()
        return user


class ChangeEmailSerializer(serializers.Serializer):
    email = serializers.EmailField()
    current_password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        user = self.context["request"].user
        if not user.check_password(attrs["current_password"]):
            raise serializers.ValidationError({"current_password": "Incorrect password."})
        if User.objects.filter(email__iexact=attrs["email"]).exclude(pk=user.pk).exists():
            raise serializers.ValidationError({"email": "That email is already in use."})
        return attrs


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, validators=[validate_password])

    def validate_current_password(self, value):
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Incorrect password.")
        return value


class OrganizerSettingsSerializer(serializers.ModelSerializer):
    # Same reasoning as OrganizerProfileSerializer — without this, `rating`
    # arrives in the frontend as a string ("0.00") instead of a number,
    # breaking any .toFixed() call on it.
    rating = serializers.DecimalField(max_digits=3, decimal_places=2, coerce_to_string=False)

    class Meta:
        model = OrganizerProfile
        fields = [
            "display_name", "bio", "is_verified", "rating",
            "events_hosted", "stripe_onboarding_complete",
        ]
        # Only display_name/bio are actually editable by the organizer —
        # verification, rating, event count, and payout status are all
        # server-computed. Exposed read-only here so ONE endpoint can back
        # the whole settings screen instead of the frontend stitching
        # together multiple partial views.
        read_only_fields = [
            "is_verified", "rating", "events_hosted", "stripe_onboarding_complete",
        ]