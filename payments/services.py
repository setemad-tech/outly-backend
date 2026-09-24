"""
payments/services.py
---------------------
All direct Stripe SDK calls live here — views stay thin and just handle
HTTP concerns. Uses Stripe Checkout Session (Stripe-hosted payment page)
rather than raw card handling, so card data never touches our server and
PCI-DSS scope stays minimal (SAQ A).
"""

import logging
from datetime import timedelta

import stripe
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from bookings.models import Booking
from bookings.services import cancel_pending_booking
from accounts.models import OrganizerProfile
from .models import Payment

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY

# --- Commission ---------------------------------------------------------
# 0 today, on purpose — no commission is actually taken yet. Flip this to
# 10 (or whatever %) via the PLATFORM_COMMISSION_PERCENT setting once
# you're ready. This is real, working logic gated behind that number, not
# a comment you'd have to rewrite later — turning commission on is a
# one-line settings change, not a code change.
#
# Uses Stripe Connect "destination charges": the full ticket price is
# charged to YOUR platform Stripe account, Stripe automatically keeps
# `application_fee_amount` for you, and transfers the remainder straight
# to the organizer's own connected Stripe account. Neither you nor Stripe
# ever needs their raw bank details for this — that's Stripe Connect
# onboarding's job, separate from this checkout flow.
PLATFORM_COMMISSION_PERCENT = getattr(settings, "PLATFORM_COMMISSION_PERCENT", 0)


def create_checkout_session(*, booking: Booking, success_url: str, cancel_url: str):
    """
    Creates a Stripe-hosted Checkout Session for a PENDING booking.

    idempotency_key uses the booking id so if the frontend retries this
    call (double-click, flaky network), Stripe returns the *same* session
    instead of creating a second one for the same booking.
    """
    session_kwargs = dict(
        mode="payment",
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": booking.event.currency.lower(),
                "unit_amount": booking.event.price_minor,
                "product_data": {"name": booking.event.title},
            },
            "quantity": booking.quantity,
        }],
        success_url=success_url,
        cancel_url=cancel_url,
        customer_email=booking.user.email,
        # Stripe's minimum lifetime, not the spot hold — see
        # CHECKOUT_HOLD_MINUTES / release_stale_holds for the 5-minute hold.
        expires_at=int(
            (timezone_now() + timedelta(minutes=settings.CHECKOUT_SESSION_EXPIRY_MINUTES)).timestamp()
        ),
        client_reference_id=str(booking.id),
        metadata={"booking_id": str(booking.id)},
        idempotency_key=f"booking-checkout-{booking.id}",
    )

    # --- Commission split (currently inert at 0%) -----------------------
    if PLATFORM_COMMISSION_PERCENT > 0:
        organizer = booking.event.organizer

        if not organizer.stripe_account_id or not organizer.stripe_onboarding_complete:
            # Fail loudly and early rather than silently taking 100% or
            # silently failing the transfer after the customer already paid.
            raise ValidationError(
                "This event's organizer hasn't finished payout setup yet — "
                "checkout is unavailable until they do."
            )

        total_amount = booking.event.price_minor * booking.quantity
        application_fee = round(total_amount * PLATFORM_COMMISSION_PERCENT / 100)

        session_kwargs["payment_intent_data"] = {
            "application_fee_amount": application_fee,
            "transfer_data": {"destination": organizer.stripe_account_id},
        }
    # ----------------------------------------------------------------------

    session = stripe.checkout.Session.create(**session_kwargs)

    Payment.objects.update_or_create(
        booking=booking,
        defaults={
            "provider": Payment.Provider.STRIPE,
            "provider_reference": session.id,
            "amount_minor": booking.price_paid_minor,
            "currency": booking.event.currency,
        },
    )
    return session


def verify_and_parse_webhook(payload: bytes, sig_header: str):
    """
    Raises stripe.error.SignatureVerificationError if the payload wasn't
    actually sent by Stripe. This check is what stops anyone from POSTing
    a fake "payment succeeded" event straight at your webhook URL — never
    skip it, even in development against test mode.
    """
    return stripe.Webhook.construct_event(
        payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
    )


@transaction.atomic
def handle_checkout_completed(session: dict):
    """
    Idempotent on purpose: Stripe can and will redeliver the same webhook
    event more than once. Re-running this for an already-CONFIRMED booking
    must be a safe no-op, not a double-charge or duplicate ticket.
    """
    booking_id = session["metadata"]["booking_id"]

    try:
        booking = Booking.objects.select_for_update().get(pk=booking_id)
    except Booking.DoesNotExist:
        return  # log this — should never happen if metadata is set correctly

    if booking.status == Booking.Status.CONFIRMED:
        return  # already processed, nothing to do

    if session["payment_status"] != "paid":
        # Checkout finished but the money hasn't actually moved yet (only
        # possible with delayed payment methods — card-only today). Don't
        # issue a ticket for an unpaid booking.
        logger.warning("Checkout completed unpaid for booking %s", booking.id)
        return

    booking.status = Booking.Status.CONFIRMED
    booking.save(update_fields=["status"])

    Payment.objects.filter(booking=booking).update(
        provider_reference=session["payment_intent"],
        paid_at=timezone_now(),
    )

    # Best-effort — the booking is already confirmed and that's the part
    # that matters. A Brevo hiccup shouldn't make Stripe think this webhook
    # failed (Stripe retries failed webhooks, which would re-run this
    # whole function — harmless here since it's idempotent, but pointless
    # if the actual cause was just the email provider being down).
    try:
        from bookings.emails import send_ticket_email
        send_ticket_email(booking)
    except Exception:
        logger.exception("Failed to send ticket email for booking %s", booking.id)


@transaction.atomic
def handle_checkout_expired(session: dict):
    booking_id = session["metadata"]["booking_id"]
    try:
        booking = Booking.objects.select_for_update().get(pk=booking_id)
    except Booking.DoesNotExist:
        return
    cancel_pending_booking(booking)


def release_abandoned_checkout(*, user, event_id):
    """
    Called before starting a new checkout. If this user already has a
    PENDING booking for the event (they backed out of Stripe, or closed the
    tab), expire its Stripe session so it can no longer be paid, then cancel
    the booking so they can start over.

    If that old session turns out to have been paid already (webhook just
    hasn't landed yet), confirm the booking right now instead — the new
    checkout attempt will then correctly fail with "already have a booking".
    """
    booking = (
        Booking.objects.filter(user=user, event_id=event_id, status=Booking.Status.PENDING)
        .select_related("payment")
        .first()
    )
    if booking is not None:
        _release_pending_booking(booking)


def release_stale_holds(*, event_id):
    """
    Called before starting any checkout for an event. PENDING bookings older
    than CHECKOUT_HOLD_MINUTES already don't count against capacity, but
    their Stripe session is still payable (Stripe won't expire it sooner
    than 30 min). Expire those sessions now, so a spot handed to the next
    person can't also be paid for by whoever abandoned it.
    """
    cutoff = timezone_now() - timedelta(minutes=settings.CHECKOUT_HOLD_MINUTES)
    stale = Booking.objects.filter(
        event_id=event_id, status=Booking.Status.PENDING, booked_at__lt=cutoff
    ).select_related("payment")
    for booking in stale:
        _release_pending_booking(booking)


def _release_pending_booking(booking: Booking):
    """
    Expire the booking's Stripe session and cancel it. If the session turns
    out to be paid already (webhook just hasn't landed yet), confirm the
    booking instead — the spot is genuinely taken.
    """
    payment = getattr(booking, "payment", None)
    session_id = payment.provider_reference if payment else ""
    if session_id.startswith("cs_"):
        try:
            stripe.checkout.Session.expire(session_id)
        except stripe.error.InvalidRequestError:
            # Session is no longer open — either already expired or completed.
            session = stripe.checkout.Session.retrieve(session_id)
            if session["status"] == "complete":
                handle_checkout_completed(session)
                return

    cancel_pending_booking(booking)


def timezone_now():
    from django.utils import timezone
    return timezone.now()


def handle_account_updated(account: dict):
    """
    Fired by Stripe as an organizer progresses through (or finishes)
    Connect onboarding. payouts_enabled is Stripe's own answer to "can
    this account actually receive transfers yet" — more reliable than
    trying to infer readiness from individual requirement fields
    ourselves, since Stripe's onboarding requirements can change.
    """
    try:
        profile = OrganizerProfile.objects.get(stripe_account_id=account["id"])
    except OrganizerProfile.DoesNotExist:
        return  # webhook for an account we don't recognize — ignore, don't error

    complete = bool(getattr(account, "payouts_enabled", False))
    if profile.stripe_onboarding_complete != complete:
        profile.stripe_onboarding_complete = complete
        profile.save(update_fields=["stripe_onboarding_complete"])