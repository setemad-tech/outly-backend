"""
events/views.py
-----------------
Just the viewset — EventSerializer lives in events/serializers.py, not here.
"""

from datetime import timedelta

from django.conf import settings
from django.db.models import Prefetch, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import viewsets, permissions

from .models import Event, TicketType
from .serializers import EventSerializer
from .permissions import IsOrganizer, IsEventOwnerOrReadOnly


class EventViewSet(viewsets.ModelViewSet):
    """
    GET  /api/events/            -> published events, PLUS the requesting
                                     organizer's own events of any status
                                     (so they can see + edit their drafts)
                                     Finished events (8h after they end) drop
                                     off this list — see Event.still_listed.
    GET  /api/events/?mine=true  -> only the requesting organizer's events,
                                     any status — powers an "My events" dashboard
    POST /api/events/            -> organizers only (IsOrganizer)
    PATCH/DELETE /api/events/id/ -> owner only (IsEventOwnerOrReadOnly)
    """
    serializer_class = EventSerializer
    permission_classes = [permissions.AllowAny, IsOrganizer, IsEventOwnerOrReadOnly]

    def get_queryset(self):
        user = self.request.user
        base = Event.objects.select_related("organizer", "category").prefetch_related(
            "perks", Prefetch("ticket_types", queryset=ticket_types_with_sold())
        )

        if self.request.query_params.get("mine") == "true":
            if not user.is_authenticated or not hasattr(user, "organizer_profile"):
                return Event.objects.none()
            return base.filter(organizer=user.organizer_profile)

        visible = Q(status=Event.Status.PUBLISHED)
        if self.action == "retrieve":
            # A shared link to a cancelled/finished event should say so,
            # not 404. Drafts stay private.
            visible |= Q(status__in=[Event.Status.CANCELLED, Event.Status.COMPLETED])
        if user.is_authenticated and hasattr(user, "organizer_profile"):
            visible |= Q(organizer=user.organizer_profile)  # see my own drafts too

        qs = base.filter(visible)
        # Only the public list hides finished events — opening one by id
        # (a ticket, a chat, a shared link) still works afterwards.
        if self.action == "list":
            qs = qs.still_listed()
        return qs


def ticket_types_with_sold():
    """Ticket types with `sold` = units held by live bookings, in one query."""
    from bookings.models import Booking

    hold_cutoff = timezone.now() - timedelta(minutes=settings.CHECKOUT_HOLD_MINUTES)
    live = Q(items__booking__status__in=[Booking.Status.CONFIRMED, Booking.Status.ATTENDED]) | Q(
        items__booking__status=Booking.Status.PENDING, items__booking__booked_at__gte=hold_cutoff
    )
    # Explicit order_by: Django drops Meta.ordering from aggregate queries.
    return (
        TicketType.objects.select_related("event")
        .annotate(sold=Coalesce(Sum("items__quantity", filter=live), 0))
        .order_by("position", "created_at")
    )
