from django.urls import path
from .views import (
    CreateCheckoutSessionView,
    CreateCryptoCheckoutView,
    PaymentMethodsView,
    StripeWebhookView,
    SyncBookingPaymentView,
)

urlpatterns = [
    path("bookings/<uuid:event_id>/checkout/", CreateCheckoutSessionView.as_view()),
    path("bookings/<uuid:pk>/sync-payment/", SyncBookingPaymentView.as_view()),
    path("bookings/<uuid:event_id>/crypto-checkout/", CreateCryptoCheckoutView.as_view()),
    path("payments/methods/", PaymentMethodsView.as_view()),
    path("payments/webhook/", StripeWebhookView.as_view()),
]