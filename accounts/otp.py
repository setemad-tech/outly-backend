"""
accounts/otp.py
----------------
Email-verification OTP: generate a 6-digit code, email it, and check what
the user typed back in. Kept separate from views.py/serializers.py because
both RegisterView (auto-send on signup) and the dedicated
send/verify views need to call into it.

Security notes:
  - The code is hashed with Django's password hasher before it touches the
    DB (make_password/check_password) — same as a real password, never
    stored in plaintext.
  - Expires after OTP_TTL_MINUTES.
  - Locked out after OTP_MAX_ATTEMPTS wrong guesses (6 digits is only
    1,000,000 possibilities; without this a script could just brute-force
    it before it expires).
"""

import logging
import secrets
from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from .models import EmailOTP

logger = logging.getLogger(__name__)

OTP_TTL_MINUTES = 10
OTP_RESEND_COOLDOWN_SECONDS = 60
OTP_MAX_ATTEMPTS = 5


class OtpError(Exception):
    """Raised with a message that's safe to show the user directly."""


def generate_and_send_otp(user) -> EmailOTP:
    code = f"{secrets.randbelow(1_000_000):06d}"
    otp, _ = EmailOTP.objects.update_or_create(
        user=user,
        defaults={
            "code_hash": make_password(code),
            "attempts": 0,
            "expires_at": timezone.now() + timedelta(minutes=OTP_TTL_MINUTES),
        },
    )
    _send_otp_email(user, code)
    return otp


def _send_otp_email(user, code: str) -> None:
    subject = "Your OUTLY verification code"
    text_body = (
        f"Your OUTLY verification code is {code}.\n\n"
        f"It expires in {OTP_TTL_MINUTES} minutes. Enter it in the app to "
        "confirm this is really your email address — we'll send your event "
        "QR tickets here, so getting it right matters.\n\n"
        "Didn't request this? You can safely ignore this email."
    )
    html_body = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 480px; margin: 0 auto; color: #111;">
      <p style="letter-spacing: 3px; font-weight: 800; font-size: 13px; margin-bottom: 4px;">OUTLY</p>
      <h2 style="margin: 8px 0 4px;">Confirm your email</h2>
      <p style="color: #666; margin: 0 0 20px;">
        Enter this code in the app to verify {user.email}.
      </p>
      <p style="font-size: 32px; font-weight: 800; letter-spacing: 8px; margin: 0 0 20px;">
        {code}
      </p>
      <p style="color: #888; font-size: 13px;">
        Expires in {OTP_TTL_MINUTES} minutes. We send your event QR tickets to this
        address, so it's worth double-checking.
      </p>
    </div>
    """
    email = EmailMultiAlternatives(subject=subject, body=text_body, to=[user.email])
    email.attach_alternative(html_body, "text/html")
    email.send(fail_silently=False)


def verify_otp(user, code: str) -> None:
    try:
        otp = user.email_otp
    except EmailOTP.DoesNotExist:
        raise OtpError("No verification code was requested. Send a new one.")

    if otp.expires_at < timezone.now():
        raise OtpError("This code has expired. Request a new one.")

    if otp.attempts >= OTP_MAX_ATTEMPTS:
        raise OtpError("Too many incorrect attempts. Request a new one.")

    if not check_password(code, otp.code_hash):
        otp.attempts += 1
        otp.save(update_fields=["attempts"])
        raise OtpError("Incorrect code.")

    user.email_verified = True
    user.save(update_fields=["email_verified"])
    otp.delete()
