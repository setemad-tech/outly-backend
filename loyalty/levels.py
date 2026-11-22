"""
loyalty/levels.py
------------------
Loyalty levels (BU-31, owner decision 2026-11-20): Explorer, Insider, Icon,
reached at 25 / 75 / 150 events someone has actually attended — checked in
at the door, counted once per event, over their whole time on OUTLY. A
level is never lost (the count only grows).

Below Explorer someone is level 0 — a member with no loyalty perks yet: no
points earned (they can still use ordinary promo codes). Every level earns
the same points (events/pricing.py LOYALTY_PERCENT, 2%), and unlocks:
  - members-only promo codes (promos.PromoCode.min_level)
  - early access to tickets on events that offer it
    (events.Event.early_access_level / early_access_hours)
  - priority support (Stage 4)

Level 0 is "" in code.
"""

from django.db import models

# (key, display name, events attended to reach it) — lowest first.
LEVELS = [
    ("explorer", "Explorer", 25),
    ("insider", "Insider", 75),
    ("icon", "Icon", 150),
]
LEVEL_KEYS = [k for k, _, _ in LEVELS]
LEVEL_NAMES = {k: name for k, name, _ in LEVELS}


class Level(models.TextChoices):
    """For model fields that name a minimum level ("" = everyone)."""
    EXPLORER = "explorer", "Explorer"
    INSIDER = "insider", "Insider"
    ICON = "icon", "Icon"


def rank(level: str) -> int:
    """0 = no level, 1 = Explorer, 2 = Insider, 3 = Icon."""
    return LEVEL_KEYS.index(level) + 1 if level in LEVEL_KEYS else 0


def level_for(events_attended: int) -> str:
    current = ""
    for key, _, needed in LEVELS:
        if events_attended >= needed:
            current = key
    return current


def events_attended(user) -> int:
    """Distinct events this person was checked in at."""
    from bookings.models import Booking

    if user is None or not getattr(user, "is_authenticated", False):
        return 0
    return (
        Booking.objects.filter(user=user, status=Booking.Status.ATTENDED)
        .values("event_id").distinct().count()
    )


def points_percent(level: str) -> int:
    """Points earned on tickets, in percent of the organizer's price: level 0 earns none."""
    from events.pricing import LOYALTY_PERCENT
    return LOYALTY_PERCENT if level in LEVEL_KEYS else 0


def user_level(user) -> str:
    if getattr(user, "is_guest", False):
        return ""  # guests (quick checkout) have no level and earn nothing
    return level_for(events_attended(user))


def meets(user_or_level, minimum: str) -> bool:
    """Whether a user (or a level key) is at `minimum` or above. "" minimum = everyone."""
    if not minimum:
        return True
    level = user_or_level if isinstance(user_or_level, str) else user_level(user_or_level)
    return rank(level) >= rank(minimum)


def summary(user) -> dict:
    """What the Rewards page shows: level, progress to the next one, all levels."""
    attended = 0 if getattr(user, "is_guest", False) else events_attended(user)
    current = level_for(attended)
    nxt = next(((k, n) for k, _, n in LEVELS if attended < n), None)
    return {
        "level": current,
        "events_attended": attended,
        "next_level": nxt[0] if nxt else "",
        "next_level_at": nxt[1] if nxt else None,
        "events_to_next": (nxt[1] - attended) if nxt else 0,
        "points_percent": points_percent(current),
        "levels_points_percent": points_percent(LEVEL_KEYS[0]),
        "levels": [{"key": k, "name": name, "events": n} for k, name, n in LEVELS],
    }
