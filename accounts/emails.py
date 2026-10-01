"""
accounts/emails.py
--------------------
Organizer-application review emails, sent when an admin approves or
rejects an application in Django admin (accounts/admin.py).
"""

import logging
from html import escape

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

from .models import OrganizerProfile

logger = logging.getLogger(__name__)


def send_organizer_review_email(profile: OrganizerProfile) -> None:
    """Tell the applicant the outcome. Never raises — the review itself already saved."""
    user = profile.user
    dashboard_url = f"{settings.FRONTEND_URL}/organizer"

    if profile.status == OrganizerProfile.Status.APPROVED:
        subject = "You're approved as an OUTLY organizer"
        heading = "You're approved!"
        text_lines = [
            f"Good news — {profile.display_name} is now an approved organizer on OUTLY.",
            "You can create events, sell tickets and check attendees in from your organizer dashboard:",
            dashboard_url,
        ]
    elif profile.status == OrganizerProfile.Status.REJECTED:
        subject = "Your OUTLY organizer application"
        heading = "We couldn't approve your application"
        text_lines = [
            f"Thanks for applying to organize events on OUTLY as {profile.display_name}.",
            "We weren't able to approve the application this time.",
        ]
        if profile.review_note.strip():
            text_lines.append(f"Reviewer's note: {profile.review_note.strip()}")
        text_lines += [
            "You can update your details and apply again from the organizer page:",
            dashboard_url,
        ]
    else:
        return

    text_body = "\n\n".join(text_lines)
    paragraphs = "".join(
        f'<p style="color: #444; margin: 0 0 14px;">{escape(line)}</p>'
        for line in text_lines[:-1]
    )
    html_body = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 480px; margin: 0 auto; color: #111;">
      <p style="letter-spacing: 3px; font-weight: 800; font-size: 13px; margin-bottom: 4px;">OUTLY</p>
      <h2 style="margin: 8px 0 16px;">{escape(heading)}</h2>
      {paragraphs}
      <p style="margin: 20px 0 0;">
        <a href="{escape(dashboard_url)}" style="display: inline-block; background: #111; color: #fff; padding: 10px 18px; border-radius: 999px; text-decoration: none; font-weight: 600;">
          Open organizer page
        </a>
      </p>
    </div>
    """
    try:
        email = EmailMultiAlternatives(subject=subject, body=text_body, to=[user.email])
        email.attach_alternative(html_body, "text/html")
        email.send(fail_silently=False)
    except Exception:
        logger.exception("Could not send organizer review email to %s", user.email)
