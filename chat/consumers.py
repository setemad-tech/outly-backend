"""
chat/consumers.py
------------------
One consumer instance per connected client per room. On connect, joins a
"channel group" named after the room id — Channels' pub/sub mechanism for
fanning a message out to every open connection for that room, potentially
across multiple server processes (this is what the Redis channel layer
is for; without it, group broadcast only works within a single process).
"""

import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async

from .models import ChatRoom, Message
from .permissions import user_can_access_room


class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_id = self.scope["url_route"]["kwargs"]["room_id"]
        self.group_name = f"chat_{self.room_id}"
        user = self.scope["user"]  # populated by TokenAuthMiddleware, see middleware.py

        allowed = await self._user_allowed(user)
        if not allowed:
            await self.close(code=4403)  # custom close code the frontend can check for "forbidden"
            return

        room_open = await self._room_open()
        if not room_open:
            await self.close(code=4410)  # "gone" — frontend can show "chat closed" rather than retrying
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data):
        """Client sent a message. Persist it, then broadcast to everyone in the room."""
        data = json.loads(text_data)
        text = (data.get("text") or "").strip()
        if not text or len(text) > 2000:
            return  # silently drop empty/oversized payloads rather than erroring the socket

        if not await self._room_open():
            # window closed while this connection was still open (e.g. user
            # left the tab open past the event) — reject the send rather
            # than silently accepting a message into a "closed" room
            await self.send(text_data=json.dumps({"error": "chat_closed"}))
            return

        user = self.scope["user"]
        message = await self._save_message(user, text)

        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "chat.message",  # routes to the chat_message() handler below
                "id": message.id,
                "text": message.text,
                "sender": user.id,
                "sender_name": user.get_full_name() or user.username,
                "sent_at": message.sent_at.isoformat(),
            },
        )

    async def chat_message(self, event):
        """Handler name maps from event["type"] "chat.message" -> chat_message."""
        await self.send(text_data=json.dumps({
            "id": event["id"],
            "text": event["text"],
            "sender": event["sender"],
            "sender_name": event["sender_name"],
            "sent_at": event["sent_at"],
        }))

    @database_sync_to_async
    def _user_allowed(self, user):
        try:
            room = ChatRoom.objects.select_related("event").get(pk=self.room_id)
        except ChatRoom.DoesNotExist:
            return False
        return user_can_access_room(user, room)

    @database_sync_to_async
    def _room_open(self):
        try:
            room = ChatRoom.objects.select_related("event").get(pk=self.room_id)
        except ChatRoom.DoesNotExist:
            return False
        return room.event.chat_is_open

    @database_sync_to_async
    def _save_message(self, user, text):
        return Message.objects.create(room_id=self.room_id, sender=user, text=text)