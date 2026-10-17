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

logger = logging.getLogger(__name__)
from .services import create_pending_booking, SoldOutError
from django.core.exceptions import ValidationError
from events.models import Event
from events.pricing import ticket_price_minor


# ---------------------------------------------------------------------------
# serializers.py
# ---------------------------------------------------------------------------

class TicketEventSerializer(serializers.Serializer):
    """Minimal nested event info — just what a ticket card needs to render."""
    id = serializers.UUIDField()
    title = serializers.CharField()
    venue_name = serializers.CharField()
    start_at = serializers.DateTimeField()
    cover_image = serializers.ImageField(allow_null=True)
    price_minor = serializers.IntegerField()
    ticket_price_minor = serializers.SerializerMethodField()
    currency = serializers.CharField()
    min_age = serializers.IntegerField(allow_null=True)
    entry_restrictions = serializers.CharField()

    def get_ticket_price_minor(self, event):
        return ticket_price_minor(event.price_minor)


class TicketSerializer(serializers.ModelSerializer):
    event = TicketEventSerializer(read_only=True)
    qr_payload = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            "id", "event", "status", "quantity", "checked_in_count", "booked_at", "qr_payload",
            "price_paid_minor", "loyalty_points", "points_redeemed",
        ]

    def get_qr_payload(self, booking):
        """
        Signed, not just the raw id — see the earlier discussion on why:
        the check-in endpoint can reject anything not actually issued by
        this server (django.core.signing.loads raises BadSignature) without
        needing a DB round-trip just to detect a tampered/forged code.
        Frontend renders this string as a QR client-side (e.g. `qrcode.react`)
        rather than the backend generating an image.
        """
        return signing.dumps(str(booking.id))