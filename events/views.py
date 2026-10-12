"""
events/views.py
-----------------
Just the viewset — EventSerializer lives in events/serializers.py, not here.
"""

from django.db.models import Q
from rest_framework import viewsets, permissions

from .models import Event
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
        base = Event.objects.select_related("organizer", "category").prefetch_related("perks")

        if self.request.query_params.get("mine") == "true":
            if not user.is_authenticated or not hasattr(user, "organizer_profile"):
                return Event.objects.none()
            return base.filter(organizer=user.organizer_profile)

        visible = Q(status=Event.Status.PUBLISHED)
        if user.is_authenticated and hasattr(user, "organizer_profile"):
            visible |= Q(organizer=user.organizer_profile)  # see my own drafts too

        qs = base.filter(visible)
        # Only the public list hides finished events — opening one by id
        # (a ticket, a chat, a shared link) still works afterwards.
        if self.action == "list":
            qs = qs.still_listed()
        return qs