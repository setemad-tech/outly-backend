"""
accounts/guests.py
-------------------
Removing guest details (owner, 2026-11-22): 45 days after a guest's last
event (or 45 days after they signed in, if they never booked), their name,
email and phone are wiped and the account is switched off. Bookings and
payments stay for refunds and accounting, just no longer tied to a person.

Runs at most once a day from the quick-checkout sign-in
(maybe_purge_guests), and on demand: `python manage.py purge_guests`.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Max
from django.utils import timezone

GUEST_RETENTION_DAYS = 45


def purge_expired_guests(now=None) -> int:
    from .models import EmailLoginCode

    User = get_user_model()
    now = now or timezone.now()
    cutoff = now - timedelta(days=GUEST_RETENTION_DAYS)
    guests = (
        User.objects.filter(is_guest=True, is_active=True, date_joined__lt=cutoff)
        .annotate(last_event=Max("bookings__event__start_at"))
    )
    removed = 0
    for user in guests:
        if user.last_event is not None and user.last_event >= cutoff:
            continue
        EmailLoginCode.objects.filter(email__iexact=user.email).delete()
        user.email = f"removed-guest-{user.pk}@guest.invalid"
        user.first_name = user.last_name = ""
        user.phone_number = user.city = ""
        user.is_active = False
        user.guest_session_ends_at = None
        user.set_unusable_password()
        user.save()
        removed += 1
    return removed


def maybe_purge_guests() -> None:
    """Cheap enough to call from a request: does the work at most once a day."""
    if cache.add("outly:guest-purge", True, timeout=24 * 3600):
        try:
            purge_expired_guests()
        except Exception:
            cache.delete("outly:guest-purge")
            raise
