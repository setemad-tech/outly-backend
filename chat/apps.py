from django.apps import AppConfig


class ChatConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "chat"

    def ready(self):
        # This import is what actually registers the @receiver in
        # chat/signals.py — without it, the signal decorator never runs,
        # and no ChatRoom is ever auto-created when an event is
        # published. The import looks "unused" (chat.signals isn't
        # referenced anywhere below) but importing it IS the entire
        # point — it's what makes Django aware the signal exists at all.
        import chat.signals  # noqa: F401