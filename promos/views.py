"""
promos/views.py
----------------
  GET        /api/me/organizer/promos/        codes on an organizer's events (read-only)
  GET/POST   /api/admin/promos/               every code (staff only create them)
  PATCH      /api/admin/promos/<id>/          pause/resume, end date, limits
  GET        /api/admin/promos/targets/       organizers + their events and ticket
                                              types, for the create form

Codes are never deleted once made — pausing keeps the history that past
orders point to.
"""

from django.db.models import Prefetch, Q
from rest_framework import generics, permissions
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import OrganizerProfile, approved_organizer
from events.models import Event, TicketType
from backoffice.models import log_action
from .models import PromoCode
from .serializers import PromoCodeSerializer, with_usage


class OrganizerPromoListView(generics.ListAPIView):
    """Codes OUTLY set up for this organizer or one of their events — to see, not edit."""
    serializer_class = PromoCodeSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = None

    def get_queryset(self):
        profile = approved_organizer(self.request.user)
        if profile is None:
            raise PermissionDenied("Your organizer account hasn't been approved yet.")
        return with_usage(
            PromoCode.objects.filter(Q(organizer=profile) | Q(event__organizer=profile))
        ).order_by("-created_at")


class _StaffScoped:
    serializer_class = PromoCodeSerializer
    permission_classes = [permissions.IsAdminUser]

    pagination_class = None

    def get_queryset(self):
        qs = PromoCode.objects.all()
        if self.request.query_params.get("scope") == "outly":
            qs = qs.filter(organizer__isnull=True)
        organizer = self.request.query_params.get("organizer")
        if organizer and organizer.isdigit():
            qs = qs.filter(organizer_id=int(organizer))
        return with_usage(qs).order_by("-created_at")


class AdminPromoListView(_StaffScoped, generics.ListCreateAPIView):
    def perform_create(self, serializer):
        promo = serializer.save()
        log_action(self.request.user, "promo.created", "promo", promo.id, promo.code,
                   label=promo.describe())


class AdminPromoDetailView(_StaffScoped, generics.RetrieveUpdateAPIView):
    http_method_names = ["get", "patch"]

    def perform_update(self, serializer):
        promo = serializer.save()
        log_action(self.request.user, "promo.updated", "promo", promo.id, promo.code,
                   changes={k: str(v) for k, v in serializer.validated_data.items()})


class AdminPromoTargetsView(APIView):
    """Approved organizers with their not-yet-finished events and ticket types."""
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        from django.utils import timezone

        events = (
            Event.objects.exclude(status=Event.Status.CANCELLED)
            .filter(Q(end_at__gte=timezone.now()) | Q(end_at__isnull=True, start_at__gte=timezone.now()))
            .order_by("start_at")
            .prefetch_related(Prefetch(
                "ticket_types",
                queryset=TicketType.objects.exclude(status=TicketType.Status.ARCHIVED).order_by("position", "created_at"),
            ))
        )
        organizers = (
            OrganizerProfile.objects.filter(status=OrganizerProfile.Status.APPROVED)
            .order_by("display_name")
            .prefetch_related(Prefetch("events", queryset=events))
        )
        return Response([
            {
                "id": o.id,
                "name": o.display_name,
                "events": [
                    {
                        "id": str(e.id),
                        "title": e.title,
                        "start_at": e.start_at,
                        "ticket_types": [
                            {"id": str(t.id), "name": t.name, "allow_promo": t.allow_promo}
                            for t in e.ticket_types.all()
                        ],
                    }
                    for e in o.events.all()
                ],
            }
            for o in organizers
        ])
