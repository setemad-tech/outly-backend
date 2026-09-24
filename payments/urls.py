from django.urls import path
from .views import StripeWebhookView, CreateCheckoutSessionView, SyncBookingPaymentView

urlpatterns = [
    path("bookings/<uuid:event_id>/checkout/", CreateCheckoutSessionView.as_view()),
    path("bookings/<uuid:pk>/sync-payment/", SyncBookingPaymentView.as_view()),
    path("payments/webhook/", StripeWebhookView.as_view()),
]