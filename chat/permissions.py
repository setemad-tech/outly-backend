"""
chat/permissions.py
--------------------
Room access rule: organizer of the event, or a user with a
CONFIRMED/ATTENDED booking for it. Used by both the REST message-history
endpoint (via has_object_permission) and the WebSocket consumer (via the
plain user_can_access_room function), so the rule is defined once, not
duplicated between HTTP and WS code paths.
"""

from rest_framework.permissions import BasePermission
from bookings.models import Booking
from .models import ChatRoom


class IsEventParticipant(BasePermission):
    def has_object_permission(self, request, view, room: ChatRoom):
        return user_can_access_room(request.user, room)


def user_can_access_room(user, room: ChatRoom) -> bool:
    if not user.is_authenticated:
        return False
    event = room.event
    if hasattr(user, "organizer_profile") and event.organizer_id == user.organizer_profile.id:
        return True
    return Booking.objects.filter(
        event=event,
        user=user,
        status__in=[Booking.Status.CONFIRMED, Booking.Status.ATTENDED],
    ).exists()