"""
chat/serializers.py + chat/views.py combined for review.

REST is used for two things WebSockets are bad at: paginated history on
first load, and the room list (with a "last message" preview) shown in
the Chats tab before any specific thread is open.
"""

from rest_framework import serializers, generics, permissions
from rest_framework.exceptions import PermissionDenied

from .models import ChatRoom, Message
from .permissions import IsEventParticipant, user_can_access_room
from .services import rooms_for_user


# ---------------------------------------------------------------------------
# serializers.py
# ---------------------------------------------------------------------------

class MessageSerializer(serializers.ModelSerializer):
    sender_name = serializers.CharField(source="sender.get_full_name", read_only=True)

    class Meta:
        model = Message
        fields = ["id", "sender", "sender_name", "text", "sent_at"]
        read_only_fields = ["id", "sender", "sent_at"]


class ChatRoomListSerializer(serializers.ModelSerializer):
    event_title = serializers.CharField(source="event.title")
    chat_closes_at = serializers.DateTimeField(source="event.chat_closes_at", read_only=True)
    is_closed = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()

    class Meta:
        model = ChatRoom
        fields = ["id", "event", "event_title", "chat_closes_at", "is_closed", "last_message"]

    def get_is_closed(self, room):
        return not room.event.chat_is_open

    def get_last_message(self, room):
        msg = room.messages.order_by("-sent_at").first()
        if not msg:
            return None
        return {"text": msg.text[:80], "sent_at": msg.sent_at, "sender_name": msg.sender.get_full_name()}

