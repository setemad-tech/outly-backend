from django.urls import path
from .views import StripeWebhookView, CreateCheckoutSessionView

urlpatterns = [
    path("bookings/<uuid:event_id>/checkout/", CreateCheckoutSessionView.as_view()),
    path("payments/webhook/", StripeWebhookView.as_view()),
]