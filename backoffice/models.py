"""
backoffice app
--------------
Staff-only API behind the admin portal (admin.outly.ae): company analytics,
organizer review, and moderation of events/users/bookings. Every change made
through it is recorded as an AdminAction so there's always a trail of who
did what.
"""

from django.conf import settings
from django.db import models


class AdminAction(models.Model):
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    action = models.CharField(max_length=60)          # e.g. "organizer.approved"
    target_type = models.CharField(max_length=30)     # "organizer" / "event" / "user"
    target_id = models.CharField(max_length=64)
    target_label = models.CharField(max_length=200, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.actor} {self.action} {self.target_label}"


def log_action(actor, action, target_type, target_id, target_label="", **details):
    return AdminAction.objects.create(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        target_label=str(target_label)[:200],
        details=details,
    )
