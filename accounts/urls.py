from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from .views import (
    RegisterView,
    MeView,
    ChangeEmailView,
    ChangePasswordView,
    LogoutView,
    SendEmailOtpView,
    VerifyEmailOtpView,
    ApplyOrganizerView,
    OrganizerSettingsView,
    ConnectStripeView,
)

urlpatterns = [
    path("auth/login/", TokenObtainPairView.as_view()),
    path("auth/refresh/", TokenRefreshView.as_view()),
    path("auth/register/", RegisterView.as_view()),
    path("auth/logout/", LogoutView.as_view()),
    path("auth/password/change/", ChangePasswordView.as_view()),
    path("auth/otp/send/", SendEmailOtpView.as_view()),
    path("auth/otp/verify/", VerifyEmailOtpView.as_view()),
    path("me/", MeView.as_view()),
    path("me/email/", ChangeEmailView.as_view()),
    path("me/apply-organizer/", ApplyOrganizerView.as_view()),
    path("me/organizer/", OrganizerSettingsView.as_view()),
    path("me/organizer/connect-stripe/", ConnectStripeView.as_view()),
]