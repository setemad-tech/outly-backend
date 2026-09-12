"""
events/permissions.py
-----------------------
Two rules, kept separate because they answer different questions:

  - IsOrganizer: "is this user allowed to create events at all?"
    (do they have an OrganizerProfile)
  - IsEventOwnerOrReadOnly: "can this user modify *this specific* event?"
    (are they the organizer who owns it — object-level, not just
    account-level)
"""

from rest_framework.permissions import BasePermission, SAFE_METHODS


class IsOrganizer(BasePermission):
    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True  # reading is handled by AllowAny at the viewset level anyway
        return request.user.is_authenticated and hasattr(request.user, "organizer_profile")


class IsEventOwnerOrReadOnly(BasePermission):
    def has_object_permission(self, request, view, event):
        if request.method in SAFE_METHODS:
            return True
        return (
            request.user.is_authenticated
            and hasattr(request.user, "organizer_profile")
            and event.organizer_id == request.user.organizer_profile.id
        )