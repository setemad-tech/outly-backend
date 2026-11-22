"""
accounts/authentication.py
---------------------------
JWT sign-in, plus the guest rule (owner, 2026-11-22): a guest from quick
checkout (accounts/email_login.py) is signed in for one hour at a time.
Their tokens are issued to expire then anyway; this makes it hold even if
a token outlives the window (e.g. refreshed), so guest_session_ends_at is
the single source of truth.
"""

from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken

GUEST_SESSION_ENDED = "Your guest session has ended. Enter your email again to continue."


def guest_session_over(user) -> bool:
    if not getattr(user, "is_guest", False):
        return False
    ends = user.guest_session_ends_at
    return ends is None or ends <= timezone.now()


class OutlyJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        if guest_session_over(user):
            raise AuthenticationFailed(GUEST_SESSION_ENDED, code="guest_session_ended")
        return user


def tokens_for(user) -> dict:
    """Access + refresh tokens; a guest's never outlive their session."""
    refresh = RefreshToken.for_user(user)
    if user.is_guest and user.guest_session_ends_at:
        left = user.guest_session_ends_at - timezone.now()
        refresh.set_exp(lifetime=left)
        access = refresh.access_token
        access.set_exp(lifetime=min(left, api_settings.ACCESS_TOKEN_LIFETIME))
    else:
        access = refresh.access_token
    return {"access": str(access), "refresh": str(refresh)}

