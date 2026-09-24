# ---------------------------------------------------------------------------
# views.py
# ---------------------------------------------------------------------------


import logging

from django.core import signing
from django.db import transaction
from django.db.models import Sum, Count, Q
from django.db.models.functions import TruncDate
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import serializers, generics, permissions, status
from rest_framework.views import APIView
from rest_framework.response import Response

from .models import Booking
from .emails import send_ticket_email
from .qr import generate_qr_png
from .serializers import TicketSerializer

logger = logging.getLogger(__name__)
from .services import create_pending_booking, SoldOutError
from django.core.exceptions import ValidationError
from events.models import Event



class MyTicketsListView(generics.ListAPIView):
    """GET /api/bookings/mine/ — only CONFIRMED/ATTENDED bookings are 'tickets'."""
    serializer_class = TicketSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return (
            Booking.objects.filter(
                user=self.request.user,
                status__in=[Booking.Status.CONFIRMED, Booking.Status.ATTENDED],
            )
            .select_related("event")
            .order_by("-booked_at")
        )


class CreateFreeBookingView(APIView):
    """
    POST /api/bookings/<event_id>/free/

    For price_minor == 0 events only — confirms immediately, no Stripe
    involved. Explicitly rejects paid events rather than silently letting
    someone attend for free by hitting the wrong endpoint.
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, event_id):
        try:
            booking = create_pending_booking(
                user=request.user,
                event_id=event_id,
                quantity=int(request.data.get("quantity", 1)),
            )
        except SoldOutError as e:
            return Response({"detail": e.messages[0]}, status=status.HTTP_409_CONFLICT)
        except ValidationError as e:
            return Response({"detail": e.messages[0]}, status=status.HTTP_400_BAD_REQUEST)

        if booking.event.price_minor > 0:
            booking.delete()  # undo the hold — this endpoint isn't for paid events
            return Response(
                {"detail": "This event isn't free. Use the checkout endpoint."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        booking.status = Booking.Status.CONFIRMED
        booking.save(update_fields=["status"])

        try:
            send_ticket_email(booking)
        except Exception:
            logger.exception("Failed to send ticket email for booking %s", booking.id)

        return Response(TicketSerializer(booking).data, status=status.HTTP_201_CREATED)


class BookingDetailView(generics.RetrieveDestroyAPIView):
    """
    GET    /api/bookings/:id/  -> used by the frontend's post-Stripe-redirect
                                   polling ("confirming your spot...") to
                                   check whether the webhook has landed yet.
    DELETE /api/bookings/:id/  -> cancel.

    DELETE here does NOT delete the row. A booking (and its linked Payment)
    is a financial record — deleting it loses the paper trail for refunds,
    disputes, and accounting. Instead this sets status=CANCELLED. That
    single status flip is also what silently frees the spot: Event.spots_taken
    already filters to status in [CONFIRMED, ATTENDED], so a cancelled
    booking stops counting against capacity automatically, no extra code
    needed for that part.

    Not handled here, on purpose — a real decision to make before launch,
    not a code gap: if this was a PAID booking, cancelling doesn't refund
    the Stripe charge. That needs an explicit policy (refund window,
    no-show fees, manual review) plus a stripe.Refund.create() call wired
    into this view once you've decided what that policy is.
    """
    serializer_class = TicketSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Booking.objects.filter(user=self.request.user).select_related("event")

    def perform_destroy(self, booking):
        booking.status = Booking.Status.CANCELLED
        booking.save(update_fields=["status"])


class ParticipantSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="user.get_full_name", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = Booking
        fields = ["id", "name", "email", "status", "quantity", "booked_at", "checked_in_at"]


class ParticipantsListView(generics.ListAPIView):
    """
    GET /api/events/<event_id>/participants/
    Organizer-only — this is the "Participants" dashboard tab, showing
    contact info alongside check-in status. Different from what the chat
    "People" tab needs (any participant should be able to see who else is
    in the room, without seeing everyone's email) — that's a separate,
    more limited endpoint if you want to build it, not this one reused
    as-is.
    """
    serializer_class = ParticipantSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        event_id = self.kwargs["event_id"]
        user = self.request.user

        if not hasattr(user, "organizer_profile"):
            return Booking.objects.none()

        return (
            Booking.objects.filter(
                event_id=event_id,
                event__organizer=user.organizer_profile,  # only this organizer's own events
                status__in=[Booking.Status.CONFIRMED, Booking.Status.ATTENDED],
            )
            .select_related("user")
            .order_by("-booked_at")
        )


class CheckInView(APIView):
    """
    POST /api/bookings/checkin/
    body: {"qr_payload": "<the signed string from the ticket QR>"}

    This is the actual missing piece behind the "Check-in (QR Scan)"
    button. Organizer-only, and further scoped to only THEIR events —
    scanning a valid ticket for someone else's event correctly fails, not
    just scanning a garbage/tampered code.

    Flow:
      1. Verify the signature — rejects tampered/forged codes without a DB
         lookup (django.core.signing raises BadSignature on mismatch).
      2. Look up the booking, confirm it belongs to THIS organizer's event.
      3. Reject if not CONFIRMED (e.g. still PENDING, or CANCELLED).
      4. Reject if already ATTENDED — a screenshotted/shared ticket can't
         be scanned twice. This, not the QR encoding itself, is what
         actually prevents ticket sharing/reuse.
      5. Mark ATTENDED + stamp checked_in_at.
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        if not hasattr(request.user, "organizer_profile"):
            return Response({"detail": "Only organizers can check in tickets."}, status=403)

        raw_payload = request.data.get("qr_payload", "")
        try:
            booking_id = signing.loads(raw_payload)
        except signing.BadSignature:
            return Response({"detail": "Invalid or tampered ticket."}, status=400)

        try:
            booking = Booking.objects.select_related("event", "user").select_for_update().get(
                pk=booking_id
            )
        except Booking.DoesNotExist:
            return Response({"detail": "Ticket not found."}, status=404)

        if booking.event.organizer_id != request.user.organizer_profile.id:
            return Response({"detail": "This ticket isn't for one of your events."}, status=403)

        # Optional — the frontend now scans from inside a specific event's
        # check-in screen (not a generic scan-anything button), and passes
        # that event's id so a ticket for a *different* one of this
        # organizer's events is rejected here rather than silently accepted.
        event_id = request.data.get("event_id")
        if event_id and str(booking.event_id) != str(event_id):
            return Response(
                {"detail": f"This ticket is for a different event ({booking.event.title})."},
                status=400,
            )

        if booking.status == Booking.Status.ATTENDED:
            return Response(
                {
                    "detail": "Already checked in.",
                    "checked_in_at": booking.checked_in_at,
                    "attendee": booking.user.get_full_name() or booking.user.email,
                },
                status=409,
            )

        if booking.status != Booking.Status.CONFIRMED:
            return Response(
                {"detail": f"Ticket is {booking.status}, not valid for entry."}, status=400
            )

        booking.status = Booking.Status.ATTENDED
        booking.checked_in_at = timezone.now()
        booking.save(update_fields=["status", "checked_in_at"])

        return Response(
            {
                "detail": "Checked in.",
                "attendee": booking.user.get_full_name() or booking.user.email,
                "event": booking.event.title,
                "checked_in_at": booking.checked_in_at,
            },
            status=200,
        )


class OrganizerAnalyticsView(APIView):
    """
    GET /api/me/organizer/analytics/

    Kept deliberately simple, per your call: revenue over time (daily
    totals from confirmed/attended bookings) and attendance vs. capacity
    per event. No date-range filtering or per-event drilldown yet — this
    is the "both, kept simple" version; add query params later if you
    need to slice by event or time window.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        profile = getattr(request.user, "organizer_profile", None)
        if profile is None:
            return Response({"detail": "Not an organizer yet."}, status=403)

        paid_statuses = [Booking.Status.CONFIRMED, Booking.Status.ATTENDED]

        # Revenue over time — daily totals, oldest first. price_paid_minor
        # is the snapshot taken at booking time (see Booking model), not a
        # live price lookup, so this stays accurate even if an event's
        # price changes after some tickets already sold.
        revenue_rows = (
            Booking.objects.filter(event__organizer=profile, status__in=paid_statuses)
            .annotate(day=TruncDate("booked_at"))
            .values("day")
            .annotate(revenue_minor=Sum("price_paid_minor"))
            .order_by("day")
        )

        # Attendance vs. capacity per event, upcoming-first.
        attendance_rows = (
            Event.objects.filter(organizer=profile)
            .annotate(
                attending=Count("bookings", filter=Q(bookings__status__in=paid_statuses))
            )
            .values("id", "title", "capacity", "attending")
            .order_by("start_at")
        )

        return Response({
            "revenue_over_time": list(revenue_rows),
            "attendance_by_event": list(attendance_rows),
        })


class TicketQRImageView(APIView):
    """
    GET /api/bookings/<pk>/qr.png/

    Deliberately AllowAny — email clients fetching an <img src="..."> don't
    send auth headers, so this can't require login. Security model here is
    the same as a real paper/PDF ticket: whoever holds the URL (email in
    their inbox) can display the QR, same as whoever holds a physical
    ticket can show it at the door. The booking id in the URL is a random
    UUID (unguessable), so this isn't meaningfully different from what
    was already being emailed as an attachment before this change.

    Generates the PNG on the fly rather than storing one — deterministic
    from the same signed payload every time (signing.dumps(str(pk))), so
    there's nothing to keep in sync or clean up.
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request, pk):
        try:
            booking = Booking.objects.get(pk=pk)
        except Booking.DoesNotExist:
            return HttpResponse(status=404)

        qr_payload = signing.dumps(str(booking.id))
        png_bytes = generate_qr_png(qr_payload)
        response = HttpResponse(png_bytes, content_type="image/png")
        response["Cache-Control"] = "public, max-age=86400"  # deterministic output — safe to cache
        return response
