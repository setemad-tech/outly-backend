"""
chat/signals.py
------------------
The missing piece: get_or_create_room() existed in services.py but nothing
ever called it. This listens for an Event being saved as PUBLISHED and
creates its ChatRoom at that moment — once, idempotently, thanks to
get_or_create.

Lives in chat/, not events/, on purpose: events must never import from
chat (the established dependency direction is chat -> events, not the
reverse), so chat listening to events' signal is the correct direction —
events has no idea chat exists.
"""

from django.db.models.signals import post_save
from django.dispatch import receiver

from events.models import Event
from .services import get_or_create_room


@receiver(post_save, sender=Event)
def create_chat_room_on_publish(sender, instance: Event, **kwargs):
    if instance.status == Event.Status.PUBLISHED:
        get_or_create_room(instance)