"""
chat/services.py
------------------
Room lookup/creation logic, kept separate from permissions.py — this file
answers "which rooms exist / which room for this event", permissions.py
answers "can this user access this specific room".
"""

from django.db.models import Q
from bookings.models import Booking
from .models import ChatRoom


def get_or_create_room(event) -> ChatRoom:
    """
    Rooms are created lazily on first access rather than via a signal on
    Event creation — avoids a room existing for draft/unpublished events
    with nothing to talk about yet.
    """
    room, _ = ChatRoom.objects.get_or_create(event=event)
    return room


def rooms_for_user(user):
    """
    All rooms a user can currently see in their Chat tab: events they
    organize, plus events they have a confirmed booking for.
    """
    booked_event_ids = Booking.objects.filter(
        user=user, status__in=[Booking.Status.CONFIRMED, Booking.Status.ATTENDED]
    ).values_list("event_id", flat=True)

    organizer_id = getattr(getattr(user, "organizer_profile", None), "id", None)

    return ChatRoom.objects.filter(
        Q(event_id__in=booked_event_ids) | Q(event__organizer_id=organizer_id)
    ).select_related("event").order_by("-event__start_at")