"""
accounts/views.py
--------------------
Views only — every serializer lives in accounts/serializers.py. Covers:
register, getProfile, changeEmail, changePassword, logout, organizer
settings, and Stripe Connect onboarding.
"""

import stripe
from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import Http404
from rest_framework import generics, status, permissions
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from .models import OrganizerProfile
from .serializers import (
    ProfileSerializer,
    OrganizerProfileSerializer,
    RegisterSerializer,
    ChangeEmailSerializer,
    ChangePasswordSerializer,
    OrganizerSettingsSerializer,
)

# Set independently here rather than relying on payments/services.py having
# already run — module-level side effects like this shouldn't depend on
# cross-app import order. Cheap to set twice; wrong/unset once is a bug.
stripe.api_key = settings.STRIPE_SECRET_KEY

User = get_user_model()


class RegisterView(generics.CreateAPIView):
    """
    POST /api/auth/register/
    Returns tokens directly so the frontend can log the user in immediately
    without a second round-trip — matches api.register()'s expected shape.
    """
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        refresh = RefreshToken.for_user(user)
        return Response(
            {"access": str(refresh.access_token), "refresh": str(refresh)},
            status=status.HTTP_201_CREATED,
        )


class MeView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/me/  -> current user's profile (api.getProfile)
    PATCH  /api/me/  -> partial profile update
    DELETE /api/me/  -> account deletion (api.deleteAccount)
    """
    serializer_class = ProfileSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user


class ChangeEmailView(APIView):
    """PATCH /api/me/email/ — requires current password as re-auth, not just a valid session."""
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request):
        serializer = ChangeEmailSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        request.user.email = serializer.validated_data["email"]
        request.user.save(update_fields=["email"])
        return Response(ProfileSerializer(request.user).data)


class ChangePasswordView(APIView):
    """
    POST /api/auth/password/change/
    Deliberately does NOT log the user out elsewhere — SimpleJWT tokens
    already issued stay valid until they naturally expire, since we're not
    using the blacklist app here. Acceptable for an MVP; note it if you
    add "log out of all devices" later, since that needs the blacklist app.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        request.user.set_password(serializer.validated_data["new_password"])
        request.user.save(update_fields=["password"])
        return Response({"detail": "Password updated."})


class ApplyOrganizerView(APIView):
    """
    POST /api/me/apply-organizer/
    Creates an OrganizerProfile for request.user if they don't have one yet.
    Idempotent: calling it again just returns the existing profile rather
    than erroring — the frontend doesn't need to check "am I already an
    organizer" before calling this.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        profile, _ = OrganizerProfile.objects.get_or_create(
            user=request.user,
            defaults={
                "display_name": request.data.get("display_name", "").strip()
                or request.user.get_full_name()
                or request.user.email,
                "bio": request.data.get("bio", ""),
            },
        )
        return Response(OrganizerProfileSerializer(profile).data, status=status.HTTP_201_CREATED)


class OrganizerSettingsView(generics.RetrieveUpdateAPIView):
    """GET/PATCH /api/me/organizer/"""
    serializer_class = OrganizerSettingsSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        profile = getattr(self.request.user, "organizer_profile", None)
        if profile is None:
            raise Http404("Not an organizer yet.")
        return profile


class ConnectStripeView(APIView):
    """
    POST /api/me/organizer/connect-stripe/
    body (optional): {"return_url": "...", "refresh_url": "..."}

    Creates a Stripe Connect Express account for this organizer if they
    don't already have one, then returns a fresh onboarding link (Stripe's
    own hosted flow — real bank details are entered there, never through
    us). Safe to call again if onboarding was abandoned partway through:
    reuses the existing stripe_account_id instead of creating a duplicate
    Connect account every time someone clicks "Connect payouts".
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        profile = getattr(request.user, "organizer_profile", None)
        if profile is None:
            return Response({"detail": "Not an organizer yet."}, status=403)

        if not profile.stripe_account_id:
            account = stripe.Account.create(
                type="express",
                email=request.user.email,
                capabilities={"transfers": {"requested": True}},
            )
            profile.stripe_account_id = account.id
            profile.save(update_fields=["stripe_account_id"])

        return_url = request.data.get("return_url", "https://outly.app/organizer/settings")
        refresh_url = request.data.get("refresh_url", return_url)

        link = stripe.AccountLink.create(
            account=profile.stripe_account_id,
            refresh_url=refresh_url,
            return_url=return_url,
            type="account_onboarding",
        )
        return Response({"onboarding_url": link.url})


class LogoutView(APIView):
    """
    POST /api/auth/logout/
    Blacklists the refresh token server-side so it can't be used again even
    if it leaked. Requires rest_framework_simplejwt.token_blacklist in
    INSTALLED_APPS + its migrations run.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        refresh = request.data.get("refresh")
        if refresh:
            try:
                RefreshToken(refresh).blacklist()
            except Exception:
                pass  # token already invalid/expired — logout should succeed regardless
        return Response(status=status.HTTP_205_RESET_CONTENT)