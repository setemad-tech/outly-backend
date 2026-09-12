from django.conf import settings
from django.db import models
from django.utils import timezone


class ChatRoom(models.Model):
    """1:1 with Event today; the separate model is what makes that easy to change."""
    event = models.OneToOneField(
        "events.Event", on_delete=models.CASCADE, related_name="chat_room"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Chat: {self.event.title}"


class Message(models.Model):
    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    text = models.TextField(max_length=2000)
    sent_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["sent_at"]
        indexes = [models.Index(fields=["room", "sent_at"])]

    def __str__(self):
        return f"{self.sender}: {self.text[:30]}"