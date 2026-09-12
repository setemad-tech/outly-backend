# ---------------------------------------------------------------------------
# views.py
# ---------------------------------------------------------------------------

from rest_framework import serializers, generics, permissions
from rest_framework.exceptions import PermissionDenied

from .models import ChatRoom, Message
from .permissions import IsEventParticipant, user_can_access_room
from .services import rooms_for_user
from .serializers import ChatRoomListSerializer, MessageSerializer


class ChatRoomListView(generics.ListAPIView):
    """GET /api/chat/rooms/  -> rooms for the Chats tab, most recent event first."""
    serializer_class = ChatRoomListSerializer
    permission_classes = [permissions.IsAuthenticated]
 
    def get_queryset(self):
        return rooms_for_user(self.request.user)
 
 
class MessageHistoryView(generics.ListAPIView):
    """
    GET /api/chat/rooms/<room_id>/messages/  -> paginated history, oldest-first
    within each page (standard DRF pagination handles page size/ordering).
    Frontend calls this once on opening a thread, then switches to the
    WebSocket for anything after that point.
    """
    serializer_class = MessageSerializer
    permission_classes = [permissions.IsAuthenticated, IsEventParticipant]
 
    def get_queryset(self):
        room = ChatRoom.objects.select_related("event").get(pk=self.kwargs["room_id"])
        self.check_object_permissions(self.request, room)
        return room.messages.select_related("sender").order_by("sent_at")