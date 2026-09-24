"""
payments/views.py
------------------
Two endpoints:
  POST /api/bookings/<event_id>/checkout/   - authenticated user starts payment
  POST /api/payments/webhook/                - Stripe calls this, not the frontend

The webhook view intentionally has NO auth/permission requirement in the
usual sense — Stripe isn't a logged-in user. Its "auth" is the signature
check in verify_and_parse_webhook(), which is why that check is mandatory,
not optional.
"""

import logging

import stripe
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework import status

from bookings.services import create_pending_booking, SoldOutError
from django.core.exceptions import ValidationError
from . import services

logger = logging.getLogger(__name__)


class CreateCheckoutSessionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, event_id):
        quantity = int(request.data.get("quantity", 1))

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
                user=request.user, event_id=event_id, quantity=quantity
            )
        except SoldOutError as e:
            return Response({"detail": e.messages[0]}, status=status.HTTP_409_CONFLICT)
        except ValidationError as e:
            return Response({"detail": e.messages[0]}, status=status.HTTP_400_BAD_REQUEST)

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
            booking.delete()
            return Response({"detail": e.messages[0]}, status=status.HTTP_400_BAD_REQUEST)
        except stripe.error.StripeError as e:
            # A genuine Stripe API failure — bad/placeholder API key,
            # amount below Stripe's per-currency minimum, account issue,
            # etc. Previously uncaught here, which meant any real Stripe
            # problem surfaced as an opaque 500 instead of a message that
            # actually explains what went wrong. Also release the held
            # spot, same as the ValidationError case above.
            booking.delete()
            message = getattr(e, "user_message", None) or str(e)
            return Response({"detail": f"Payment setup failed: {message}"}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"checkout_url": session.url}, status=status.HTTP_201_CREATED)


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