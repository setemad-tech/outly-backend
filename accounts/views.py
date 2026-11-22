"""
accounts/views.py
--------------------
Views only — every serializer lives in accounts/serializers.py. Covers:
register, getProfile, changeEmail, changePassword, logout, organizer
settings, and Stripe Connect onboarding.
"""

import logging

import stripe
from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import Http404
from django.utils import timezone
from rest_framework import generics, status, permissions
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from .models import OrganizerProfile, approved_organizer
from .otp import OTP_RESEND_COOLDOWN_SECONDS, OtpError, generate_and_send_otp, verify_otp
from .email_login import CooldownError, request_code, verify_code
from .password_reset import request_reset, reset_password
from .serializers import (
    ProfileSerializer,
    OrganizerProfileSerializer,
    OrganizerApplicationSerializer,
    RegisterSerializer,
    ChangeEmailSerializer,
    ChangePasswordSerializer,
    OrganizerSettingsSerializer,
    PasswordResetRequestSerializer,
    PasswordResetConfirmSerializer,
    GuestUpgradeSerializer,
)

logger = logging.getLogger(__name__)

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

        # Best-effort: a Brevo hiccup shouldn't fail the whole signup and
        # strand someone with no account — they can hit /auth/otp/send/ to
        # retry once they're signed in. email_verified simply stays False
        # either way, and bookings/services.py refuses to book until it's True.
        try:
            generate_and_send_otp(user)
        except Exception:
            logger.exception("Failed to send signup verification email to %s", user.email)

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
        # A changed email is an unverified one until proven otherwise —
        # the old address's verification says nothing about the new one,
        # and QR tickets go wherever `email` currently points.
        request.user.email_verified = False
        request.user.save(update_fields=["email", "email_verified"])

        try:
            generate_and_send_otp(request.user)
        except Exception:
            logger.exception("Failed to send verification email to %s", request.user.email)

        return Response(ProfileSerializer(request.user, context={"request": request}).data)


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


class PasswordResetRequestView(APIView):
    """
    POST /api/auth/password/reset/ — body: {"email": "..."}
    Emails a reset code if the account exists. Always answers the same way,
    so it can't be used to find out who has an account.
    """
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        request_reset(serializer.validated_data["email"])
        return Response(
            {"detail": "If that email has an OUTLY account, we've sent it a reset code."}
        )


class PasswordResetConfirmView(APIView):
    """
    POST /api/auth/password/reset/confirm/ — body: {email, code, new_password}
    The frontend signs in with the new password afterwards.
    """
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            reset_password(data["email"], data["code"], data["new_password"])
        except OtpError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"detail": "Password updated."})


class EmailCodeRequestView(APIView):
    """
    POST /api/auth/email-code/ {"email"} — quick checkout from an event
    link: emails a 6-digit sign-in code (accounts/email_login.py).
    """
    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_scope = "email_code"

    def get_throttles(self):
        from rest_framework.throttling import ScopedRateThrottle
        return [ScopedRateThrottle()]

    def post(self, request):
        from rest_framework import serializers as drf

        field = drf.EmailField()
        try:
            email = field.run_validation(str(request.data.get("email", "")).strip())
        except drf.ValidationError:
            return Response({"detail": "Enter a valid email address."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            request_code(email)
        except CooldownError as e:
            return Response({"detail": str(e)}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        except Exception:
            logger.exception("Failed to send sign-in code to %s", email)
            return Response(
                {"detail": "Couldn't send the code. Check the email address and try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response({"detail": "Code sent."})


class EmailCodeVerifyView(APIView):
    """
    POST /api/auth/email-code/verify/ {"email", "code"}
      -> {"access", "refresh", "created", "guest", "guest_session_ends_at"}.
    Signs in to the existing account for that email, or as a 1-hour guest.
    """
    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_scope = "email_code_verify"

    def get_throttles(self):
        from rest_framework.throttling import ScopedRateThrottle
        return [ScopedRateThrottle()]

    def post(self, request):
        from .authentication import tokens_for
        from .guests import maybe_purge_guests

        email = str(request.data.get("email", "")).strip()
        code = str(request.data.get("code", "")).strip()
        if not email or not code:
            return Response({"detail": "Email and code are required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            user, created = verify_code(email, code)
        except OtpError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])
        try:
            maybe_purge_guests()  # once a day, see accounts/guests.py
        except Exception:
            logger.exception("Guest clean-up failed")
        return Response({
            **tokens_for(user),
            "created": created,
            "guest": user.is_guest,
            "guest_session_ends_at": user.guest_session_ends_at.isoformat() if user.is_guest else None,
        })


class GuestUpgradeView(APIView):
    """
    POST /api/me/upgrade/ {name, password, gender} — a guest (quick
    checkout, still inside their 1-hour session, so they've just proved the
    inbox) becomes a normal OUTLY account, keeping every ticket. Returns
    fresh tokens without the guest time limit.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from .authentication import tokens_for

        user = request.user
        if not user.is_guest:
            return Response({"detail": "You already have an account."}, status=status.HTTP_400_BAD_REQUEST)
        ser = GuestUpgradeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        first_name, _, last_name = ser.validated_data["name"].strip().partition(" ")
        user.first_name, user.last_name = first_name, last_name
        user.gender = ser.validated_data["gender"]
        user.set_password(ser.validated_data["password"])
        user.is_guest = False
        user.guest_session_ends_at = None
        user.save()
        return Response({**tokens_for(user), "profile": ProfileSerializer(user, context={"request": request}).data})


class SendEmailOtpView(APIView):
    """
    POST /api/auth/otp/send/
    (Re)sends a verification code to request.user's current email. Used
    both for "code didn't arrive, resend" and as the entry point the
    frontend calls when it discovers an unverified account (e.g. logging
    back in without ever having verified).
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = request.user
        if user.email_verified:
            return Response({"detail": "Email already verified."})

        existing = getattr(user, "email_otp", None)
        if existing is not None:
            elapsed = (timezone.now() - existing.created_at).total_seconds()
            if elapsed < OTP_RESEND_COOLDOWN_SECONDS:
                wait = int(OTP_RESEND_COOLDOWN_SECONDS - elapsed)
                return Response(
                    {"detail": f"Please wait {wait}s before requesting another code."},
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )

        try:
            generate_and_send_otp(user)
        except Exception:
            logger.exception("Failed to send OTP email to %s", user.email)
            return Response(
                {"detail": "Couldn't send the verification email. Try again shortly."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response({"detail": "Verification code sent."})


class VerifyEmailOtpView(APIView):
    """POST /api/auth/otp/verify/ — body: {"code": "123456"}"""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = request.user
        if user.email_verified:
            return Response(ProfileSerializer(user, context={"request": request}).data)

        code = str(request.data.get("code", "")).strip()
        if not code:
            return Response({"detail": "Code is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            verify_otp(user, code)
        except OtpError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(ProfileSerializer(user, context={"request": request}).data)


class ApplyOrganizerView(APIView):
    """
    POST /api/me/apply-organizer/
    body: {display_name, bio?, city?, instagram?, website?, event_types?}

    Submits an organizer application for manual review — it never grants
    organizer access by itself. The profile starts PENDING and an admin
    approves or rejects it in Django admin (accounts/admin.py).

      - no profile yet  -> creates a PENDING one (201)
      - PENDING         -> updates the application details (200)
      - REJECTED        -> updates the details and re-submits as PENDING (200)
      - APPROVED        -> no-op, returns the profile (200)
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        profile = getattr(request.user, "organizer_profile", None)
        if profile is not None and profile.status == OrganizerProfile.Status.APPROVED:
            return Response(OrganizerProfileSerializer(profile, context={"request": request}).data)

        serializer = OrganizerApplicationSerializer(profile, data=request.data)
        serializer.is_valid(raise_exception=True)

        if profile is None:
            profile = serializer.save(user=request.user)
            logger.info("Organizer application submitted by %s", request.user.email)
            return Response(OrganizerProfileSerializer(profile, context={"request": request}).data, status=status.HTTP_201_CREATED)

        if profile.status == OrganizerProfile.Status.REJECTED:
            logger.info("Organizer application re-submitted by %s", request.user.email)
        profile = serializer.save(
            status=OrganizerProfile.Status.PENDING,
            review_note="",
            reviewed_at=None,
            reviewed_by=None,
        )
        return Response(OrganizerProfileSerializer(profile, context={"request": request}).data)


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
        profile = approved_organizer(request.user)
        if profile is None:
            return Response({"detail": "Your organizer account hasn't been approved yet."}, status=403)

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