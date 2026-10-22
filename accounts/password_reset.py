"""
accounts/password_reset.py
---------------------------
"Forgot password": email a 6-digit code, then swap it (plus a new password)
for a reset. Same hashing / expiry / attempt-limit rules as accounts/otp.py.

Anyone can call these without signing in, so neither step may reveal
whether an email has an account: request_reset() is silent for unknown
emails and during the resend cooldown, and reset_password() gives the
same error for an unknown email as for a wrong code.
"""

import logging
import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from .models import PasswordResetCode
from .otp import OTP_MAX_ATTEMPTS, OTP_RESEND_COOLDOWN_SECONDS, OTP_TTL_MINUTES, OtpError

logger = logging.getLogger(__name__)

User = get_user_model()

_INVALID = "Incorrect or expired code. Request a new one if it keeps failing."


def request_reset(email: str) -> None:
    user = User.objects.filter(email__iexact=email.strip(), is_active=True).first()
    if user is None:
        return

    now = timezone.now()
    existing = PasswordResetCode.objects.filter(user=user).first()
    if existing and (now - existing.sent_at).total_seconds() < OTP_RESEND_COOLDOWN_SECONDS:
        return

    code = f"{secrets.randbelow(1_000_000):06d}"
    PasswordResetCode.objects.update_or_create(
        user=user,
        defaults={
            "code_hash": make_password(code),
            "attempts": 0,
            "sent_at": now,
            "expires_at": now + timedelta(minutes=OTP_TTL_MINUTES),
        },
    )
    try:
        _send_reset_email(user, code)
    except Exception:
        # Still look like success to the caller; it's logged for us.
        logger.exception("Failed to send password reset email to %s", user.email)


def reset_password(email: str, code: str, new_password: str) -> User:
    """Raises OtpError on any failure. The caller validates new_password first."""
    user = User.objects.filter(email__iexact=email.strip(), is_active=True).first()
    reset = PasswordResetCode.objects.filter(user=user).first() if user else None
    if reset is None or reset.expires_at < timezone.now():
        raise OtpError(_INVALID)
    if reset.attempts >= OTP_MAX_ATTEMPTS:
        raise OtpError("Too many incorrect attempts. Request a new code.")
    if not check_password(code, reset.code_hash):
        reset.attempts += 1
        reset.save(update_fields=["attempts"])
        raise OtpError(_INVALID)

    user.set_password(new_password)
    # They just proved they can read this inbox.
    user.email_verified = True
    user.save(update_fields=["password", "email_verified"])
    reset.delete()
    return user


def _send_reset_email(user, code: str) -> None:
    subject = "Reset your OUTLY password"
    text_body = (
        f"Your OUTLY password reset code is {code}.\n\n"
        f"It expires in {OTP_TTL_MINUTES} minutes. Enter it on the sign-in page "
        "together with your new password.\n\n"
        "Didn't ask to reset your password? You can safely ignore this email — "
        "your password stays the same."
    )
    html_body = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 480px; margin: 0 auto; color: #111;">
      <p style="letter-spacing: 3px; font-weight: 800; font-size: 13px; margin-bottom: 4px;">OUTLY</p>
      <h2 style="margin: 8px 0 4px;">Reset your password</h2>
      <p style="color: #666; margin: 0 0 20px;">
        Enter this code on the sign-in page to choose a new password for {user.email}.
      </p>
      <p style="font-size: 32px; font-weight: 800; letter-spacing: 8px; margin: 0 0 20px;">
        {code}
      </p>
      <p style="color: #888; font-size: 13px;">
        Expires in {OTP_TTL_MINUTES} minutes. Didn't ask for this? Ignore this email —
        your password stays the same.
      </p>
    </div>
    """
    email = EmailMultiAlternatives(subject=subject, body=text_body, to=[user.email])
    email.attach_alternative(html_body, "text/html")
    email.send(fail_silently=False)
