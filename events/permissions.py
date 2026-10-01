"""
events/permissions.py
-----------------------
Two rules, kept separate because they answer different questions:

  - IsOrganizer: "is this user allowed to create events at all?"
    (do they have an OrganizerProfile that an admin has APPROVED)
  - IsEventOwnerOrReadOnly: "can this user modify *this specific* event?"
    (are they the organizer who owns it — object-level, not just
    account-level)
"""

from rest_framework.permissions import BasePermission, SAFE_METHODS

from accounts.models import approved_organizer


class IsOrganizer(BasePermission):
    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True  # reading is handled by AllowAny at the viewset level anyway
        return approved_organizer(request.user) is not None


class IsEventOwnerOrReadOnly(BasePermission):
    def has_object_permission(self, request, view, event):
        if request.method in SAFE_METHODS:
            return True
        profile = approved_organizer(request.user)
        return profile is not None and event.organizer_id == profile.id