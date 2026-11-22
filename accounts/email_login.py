"""
accounts/email_login.py
------------------------
Quick checkout (owner, 2026-11-22): someone opening a shared event link can
book with just their email. They ask for a 6-digit code, type it back, and
are signed in:
  - an OUTLY account already exists for that email -> they're signed in to
    it, so the booking lands in their own "My tickets";
  - otherwise -> a temporary GUEST, signed in for GUEST_SESSION_MINUTES
    (no password, no points, no level; details removed 45 days after their
    last event — accounts/guests.py). Asking again later starts a new
    guest session on the same guest, so all their tickets stay together.
The ticket email then goes to that address as for any booking, with a link
to view the ticket online (they have no account to sign back in to).

Same rules as the other codes (accounts/otp.py): hashed, 10-minute expiry,
60 s resend cooldown, 5 wrong tries. Staff accounts can't use it — they
sign in with their password (request is silently ignored, verify fails).
"""

import logging
import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import EmailLoginCode
from .otp import OTP_MAX_ATTEMPTS, OTP_RESEND_COOLDOWN_SECONDS, OTP_TTL_MINUTES, OtpError

logger = logging.getLogger(__name__)
User = get_user_model()

_INVALID = "Incorrect or expired code. Request a new one if it keeps failing."
GUEST_SESSION_MINUTES = 60


class CooldownError(OtpError):
    def __init__(self, wait: int):
        self.wait = wait
        super().__init__(f"Please wait {wait}s before requesting another code.")


def _existing_user(email: str):
    return User.objects.filter(email__iexact=email).first()


def _blocked(user) -> bool:
    return user is not None and (not user.is_active or user.is_staff or user.is_superuser)


def request_code(email: str) -> None:
    """Emails a sign-in code. Raises CooldownError if one was sent under a minute ago."""
    email = User.objects.normalize_email(email.strip()).lower()
    now = timezone.now()
    existing = EmailLoginCode.objects.filter(email=email).first()
    if existing and (now - existing.sent_at).total_seconds() < OTP_RESEND_COOLDOWN_SECONDS:
        raise CooldownError(int(OTP_RESEND_COOLDOWN_SECONDS - (now - existing.sent_at).total_seconds()) + 1)
    if _blocked(_existing_user(email)):
        return  # looks the same as success; staff use their password

    code = f"{secrets.randbelow(1_000_000):06d}"
    EmailLoginCode.objects.update_or_create(
        email=email,
        defaults={
            "code_hash": make_password(code),
            "attempts": 0,
            "sent_at": now,
            "expires_at": now + timedelta(minutes=OTP_TTL_MINUTES),
        },
    )
    _send_code_email(email, code)


def verify_code(email: str, code: str):
    """Returns (user, created). Raises OtpError with a message safe to show."""
    email = User.objects.normalize_email(email.strip()).lower()
    row = EmailLoginCode.objects.filter(email=email).first()
    if row is None or row.expires_at < timezone.now():
        raise OtpError(_INVALID)
    if row.attempts >= OTP_MAX_ATTEMPTS:
        raise OtpError("Too many incorrect attempts. Request a new code.")
    if not check_password(code, row.code_hash):
        row.attempts += 1
        row.save(update_fields=["attempts"])
        raise OtpError(_INVALID)

    user = _existing_user(email)
    if _blocked(user):
        raise OtpError(_INVALID)
    created = False
    if user is None:
        try:
            with transaction.atomic():
                user = User(email=email, email_verified=True, is_guest=True)
                user.set_unusable_password()
                user.save()
                created = True
        except IntegrityError:  # two tabs verifying at once
            user = _existing_user(email)
    if user.is_guest:
        user.guest_session_ends_at = timezone.now() + timedelta(minutes=GUEST_SESSION_MINUTES)
        user.save(update_fields=["guest_session_ends_at"])
    elif not user.email_verified:
        # They just proved they can read this inbox.
        user.email_verified = True
        user.save(update_fields=["email_verified"])
    row.delete()
    return user, created


def _send_code_email(email: str, code: str) -> None:
    subject = "Your OUTLY code"
    text_body = (
        f"Your OUTLY code is {code}.\n\n"
        f"It expires in {OTP_TTL_MINUTES} minutes. Enter it on the event page to "
        "continue booking — your tickets will be sent to this email.\n\n"
        "Didn't ask for this? You can safely ignore this email."
    )
    html_body = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 480px; margin: 0 auto; color: #111;">
      <p style="letter-spacing: 3px; font-weight: 800; font-size: 13px; margin-bottom: 4px;">OUTLY</p>
      <h2 style="margin: 8px 0 4px;">Your code</h2>
      <p style="color: #666; margin: 0 0 20px;">
        Enter this code on the event page to continue booking. Your tickets will be sent to {email}.
      </p>
      <p style="font-size: 32px; font-weight: 800; letter-spacing: 8px; margin: 0 0 20px;">
        {code}
      </p>
      <p style="color: #888; font-size: 13px;">
        Expires in {OTP_TTL_MINUTES} minutes. Didn't ask for this? You can safely ignore this email.
      </p>
    </div>
    """
    message = EmailMultiAlternatives(subject=subject, body=text_body, to=[email])
    message.attach_alternative(html_body, "text/html")
    message.send(fail_silently=False)
