"""Token refresh that also ends a guest's session on time (see accounts/authentication.py)."""

from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from .authentication import GUEST_SESSION_ENDED, guest_session_over


class GuestAwareRefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs):
        from django.contrib.auth import get_user_model

        data = super().validate(attrs)
        user_id = RefreshToken(attrs["refresh"]).get(api_settings.USER_ID_CLAIM)
        user = get_user_model().objects.filter(**{api_settings.USER_ID_FIELD: user_id}).first()
        if user is not None and guest_session_over(user):
            raise AuthenticationFailed(GUEST_SESSION_ENDED, code="guest_session_ended")
        return data


class OutlyTokenRefreshView(TokenRefreshView):
    serializer_class = GuestAwareRefreshSerializer
