"""
payments/views.py
------------------
Three endpoints:
  POST /api/bookings/<event_id>/checkout/   - authenticated user starts payment
  POST /api/bookings/<pk>/sync-payment/     - post-payment page confirms with Stripe
  POST /api/payments/webhook/                - Stripe calls this, not the frontend

The webhook view intentionally has NO auth/permission requirement in the
usual sense — Stripe isn't a logged-in user. Its "auth" is the signature
check in verify_and_parse_webhook(), which is why that check is mandatory,
not optional.
"""

import logging

import stripe
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework import status

from bookings.models import Booking
from bookings.serializers import TicketSerializer
from bookings.services import create_pending_booking, discard_pending_booking, SoldOutError
from django.core.exceptions import ValidationError
from . import crypto, services

logger = logging.getLogger(__name__)


class CreateCheckoutSessionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, event_id):
        quantity = request.data.get("quantity", 1)  # validated in create_pending_booking

        try:
            # A previous attempt the user backed out of would otherwise block
            # this one ("already have a booking") until it expired.
            services.release_abandoned_checkout(user=request.user, event_id=event_id)
            # Frees spots whose 5-minute hold ran out, before capacity is checked.
            services.release_stale_holds(event_id=event_id)
        except stripe.error.StripeError:
            logger.exception("Could not release abandoned checkout for event %s", event_id)
            return Response(
                {"detail": "Couldn't reach the payment provider — please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        try:
            booking = create_pending_booking(
                user=request.user, event_id=event_id, quantity=quantity,
                use_points=bool(request.data.get("use_points")),
            )
        except SoldOutError as e:
            return Response({"detail": e.messages[0]}, status=status.HTTP_409_CONFLICT)
        except ValidationError as e:
            return Response({"detail": e.messages[0]}, status=status.HTTP_400_BAD_REQUEST)

        if booking.price_paid_minor == 0:
            # Loyalty points covered the whole order — nothing to charge.
            services.confirm_booking(booking)
            return Response({"ticket": TicketSerializer(booking).data}, status=status.HTTP_201_CREATED)

        raw_success_url = request.data.get(
            "success_url", "https://outly.app/tickets/pending"
        )
        # The frontend's polling page needs to know which booking to check —
        # append it here rather than trusting the frontend to have already
        # included it, since it doesn't know the booking id until this
        # response.
        separator = "&" if "?" in raw_success_url else "?"
        success_url = f"{raw_success_url}{separator}booking_id={booking.id}"

        try:
            session = services.create_checkout_session(
                booking=booking,
                success_url=success_url,
                cancel_url=request.data.get(
                    "cancel_url", "https://outly.app/events/" + str(event_id)
                ),
            )
        except ValidationError as e:
            # Most likely PLATFORM_COMMISSION_PERCENT > 0 and this
            # organizer hasn't finished Stripe Connect onboarding yet.
            # Release the spot we just held — don't leave a PENDING
            # booking sitting around for a checkout that can never happen.
            discard_pending_booking(booking)
            return Response({"detail": e.messages[0]}, status=status.HTTP_400_BAD_REQUEST)
        except stripe.error.StripeError as e:
            # A genuine Stripe API failure — bad/placeholder API key,
            # amount below Stripe's per-currency minimum, account issue,
            # etc. Previously uncaught here, which meant any real Stripe
            # problem surfaced as an opaque 500 instead of a message that
            # actually explains what went wrong. Also release the held
            # spot, same as the ValidationError case above.
            discard_pending_booking(booking)
            message = getattr(e, "user_message", None) or str(e)
            return Response({"detail": f"Payment setup failed: {message}"}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"checkout_url": session.url}, status=status.HTTP_201_CREATED)


class SyncBookingPaymentView(APIView):
    """
    Polled by the frontend's /tickets/pending page after Stripe redirects
    back. Checks the checkout with Stripe itself, so the ticket is confirmed
    as soon as payment went through rather than whenever the webhook lands.
    The webhook still does the same thing on its own — whichever runs first
    wins, the other is a no-op.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        booking = get_object_or_404(
            Booking.objects.select_related("event", "payment"), pk=pk, user=request.user
        )
        try:
            services.sync_booking_from_stripe(booking)
        except stripe.error.StripeError:
            # Not fatal — the webhook will still confirm it. Report the
            # booking as it stands and let the page poll again.
            logger.exception("Stripe sync failed for booking %s", booking.id)
        booking.refresh_from_db()
        return Response(TicketSerializer(booking).data)


@method_decorator(csrf_exempt, name="dispatch")
class StripeWebhookView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []  # Stripe sends no session/token — signature IS the auth

    def post(self, request):
        payload = request.body
        sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")

        try:
            event = services.verify_and_parse_webhook(payload, sig_header)
        except (ValueError, stripe.error.SignatureVerificationError):
            return Response(status=status.HTTP_400_BAD_REQUEST)

        if event["type"] == "checkout.session.completed":
            services.handle_checkout_completed(event["data"]["object"])
        elif event["type"] == "checkout.session.expired":
            services.handle_checkout_expired(event["data"]["object"])
        elif event["type"] == "account.updated":
            services.handle_account_updated(event["data"]["object"])
        # Other event types (charge.refunded, etc.) can be added here later
        # as separate elif branches — the pattern doesn't change.

        return Response(status=status.HTTP_200_OK)

class PaymentMethodsView(APIView):
    """
    GET /api/payments/methods/ — which ways to pay are switched on, so the
    checkout UI can show crypto as "coming soon" until Binance Pay is set up.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        return Response([
            {"id": "card", "label": "Card", "enabled": True},
            {"id": "crypto", "label": "Crypto (Binance Pay)", "enabled": crypto.is_configured()},
        ])


class CreateCryptoCheckoutView(APIView):
    """
    POST /api/bookings/<event_id>/crypto-checkout/
    Not live yet — answers 503 until payments/crypto.py is finished and
    configured. Kept as a real endpoint so the frontend flow is already wired.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, event_id):
        if not crypto.is_configured():
            return Response(
                {"detail": "Crypto payments aren't available yet — please pay by card."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        try:
            booking = create_pending_booking(
                user=request.user, event_id=event_id, quantity=request.data.get("quantity", 1),
                use_points=bool(request.data.get("use_points")),
            )
        except SoldOutError as e:
            return Response({"detail": e.messages[0]}, status=status.HTTP_409_CONFLICT)
        except ValidationError as e:
            return Response({"detail": e.messages[0]}, status=status.HTTP_400_BAD_REQUEST)
        if booking.price_paid_minor == 0:
            services.confirm_booking(booking)  # points covered it all
            return Response({"ticket": TicketSerializer(booking).data}, status=status.HTTP_201_CREATED)
        try:
            url = crypto.create_crypto_checkout(
                booking=booking,
                success_url=request.data.get("success_url", ""),
                cancel_url=request.data.get("cancel_url", ""),
            )
        except crypto.CryptoPaymentsUnavailable as e:
            discard_pending_booking(booking)
            return Response({"detail": str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"checkout_url": url}, status=status.HTTP_201_CREATED)
