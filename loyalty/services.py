from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.db.models.functions import Coalesce

from .models import LoyaltyEntry


def balance(user) -> int:
    return LoyaltyEntry.objects.filter(user=user).aggregate(
        total=Coalesce(Sum("points"), 0)
    )["total"]


def _add_once(**fields) -> bool:
    """Creates the entry unless this booking already has one of that kind."""
    try:
        with transaction.atomic():
            LoyaltyEntry.objects.create(**fields)
        return True
    except IntegrityError:
        return False


def award_for_booking(booking) -> bool:
    """Called when a paid booking is confirmed. Safe to call repeatedly."""
    if booking.loyalty_points <= 0:
        return False
    return _add_once(
        user=booking.user, booking=booking, kind=LoyaltyEntry.Kind.EARNED,
        points=booking.loyalty_points, note=booking.event.title[:200],
    )


def reverse_for_booking(booking) -> bool:
    """Called when a confirmed booking is cancelled: takes back what it earned."""
    earned = LoyaltyEntry.objects.filter(booking=booking, kind=LoyaltyEntry.Kind.EARNED).first()
    if earned is None:
        return False
    return _add_once(
        user=booking.user, booking=booking, kind=LoyaltyEntry.Kind.REVERSED,
        points=-earned.points, note=booking.event.title[:200],
    )


def redeem_for_booking(booking) -> None:
    """Spends booking.points_redeemed. Call inside the transaction that holds the user's row lock."""
    if booking.points_redeemed > 0:
        LoyaltyEntry.objects.create(
            user=booking.user, booking=booking, kind=LoyaltyEntry.Kind.REDEEMED,
            points=-booking.points_redeemed, note=booking.event.title[:200],
        )


def return_redeemed(booking) -> bool:
    """Gives back points spent on a booking that never got paid. Safe to call repeatedly."""
    if booking.points_redeemed <= 0:
        return False
    if not LoyaltyEntry.objects.filter(booking=booking, kind=LoyaltyEntry.Kind.REDEEMED).exists():
        return False
    return _add_once(
        user=booking.user, booking=booking, kind=LoyaltyEntry.Kind.RETURNED,
        points=booking.points_redeemed, note=booking.event.title[:200],
    )


def adjust(*, user, points: int, note: str, by) -> LoyaltyEntry:
    """Staff correction. Can't take a balance below zero."""
    if points == 0:
        raise ValueError("Enter a number of points other than 0.")
    if balance(user) + points < 0:
        raise ValueError("That would take the balance below 0.")
    return LoyaltyEntry.objects.create(
        user=user, points=points, kind=LoyaltyEntry.Kind.ADJUSTED, note=note[:200], created_by=by,
    )
