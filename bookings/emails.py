"""
bookings/emails.py
---------------------
Sends the ticket confirmation email — event details, payment confirmation,
and a QR code, whether the booking was free or paid.

QR delivery: a hosted image URL (bookings/views.py's TicketQRImageView),
NOT an inline CID attachment — Brevo's API doesn't support inline
attachments at all (Anymail raises AnymailUnsupportedFeature for it). A
hosted URL works with every ESP.

Split into build_ticket_email_content() + send_ticket_email() specifically
so you can preview the real content without sending anything.
"""

from django.conf import settings
from django.utils.html import escape
from django.core.mail import EmailMultiAlternatives

from .models import Booking


def build_ticket_email_content(booking: Booking) -> tuple[str, str, str]:
    """Returns (subject, text_body, html_body) — no sending, just content."""
    event = booking.event
    reference = str(booking.id)[:8].upper()
    amount_display = f"{booking.price_paid_minor / 100:.2f} {event.currency}"
    when_display = event.start_at.strftime("%d %b %Y, %H:%M")

    qr_url = f"{settings.SITE_URL}/api/bookings/{booking.id}/qr.png/"

    # One QR admits the whole booking — say so, same as the in-app ticket.
    holder = booking.user.get_full_name() or booking.user.email
    extra = booking.quantity - 1
    holder_display = f"{holder} + {extra}" if extra else holder
    admits_html = (
        f'<p style="margin: 4px 0 0; font-size: 13px; font-weight: 700;">'
        f"Admits {booking.quantity} people</p>"
        if extra else ""
    )
    admits_text = f"Admits {booking.quantity} people\n" if extra else ""

    requirements = event.entry_requirements_display
    requirements_html = (
        f'<p style="margin: 0 0 20px; font-size: 14px;">Entry requirements: '
        f"<strong>{escape(requirements)}</strong></p>"
        if requirements else ""
    )
    requirements_text = f"Entry requirements: {requirements}\n" if requirements else ""

    html_body = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 480px; margin: 0 auto; color: #111;">
      <p style="letter-spacing: 3px; font-weight: 800; font-size: 13px; margin-bottom: 4px;">OUTLY</p>
      <h2 style="margin: 8px 0 4px;">{event.title}</h2>
      <p style="color: #666; margin: 0 0 2px;">{event.venue_name}</p>
      <p style="color: #666; margin: 0 0 20px;">{when_display}</p>
      <p style="margin: 0 0 20px; font-size: 14px;">
        Payment confirmed: <strong>{amount_display}</strong>
      </p>
      {requirements_html}
      <img src="{qr_url}" alt="Your ticket QR code" width="220" height="220" style="display:block;" />
      <p style="margin: 16px 0 0; font-size: 14px; font-weight: 600;">Ticket for</p>
      <p style="margin: 2px 0 0; font-size: 14px; color: #666;">{holder_display}</p>
      {admits_html}
      <p style="color: #888; font-size: 13px; margin-top: 20px;">
        Show this QR code at the entrance.<br>
        Booking reference: {reference}
      </p>
    </div>
    """
    text_body = (
        f"Your ticket for {event.title}\n"
        f"{event.venue_name} — {when_display}\n"
        f"Payment confirmed: {amount_display}\n"
        f"{requirements_text}"
        f"Ticket for: {holder_display}\n"
        f"{admits_text}"
        f"Booking reference: {reference}\n"
        f"QR code: {qr_url}\n"
        "Show your QR ticket in the OUTLY app at the entrance."
    )

    subject = f"Your ticket — {event.title}"
    return subject, text_body, html_body


def send_ticket_email(booking: Booking) -> None:
    subject, text_body, html_body = build_ticket_email_content(booking)

    email = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        to=[booking.user.email],
        from_email=settings.TICKETS_FROM_EMAIL,
    )
    email.attach_alternative(html_body, "text/html")
    email.send(fail_silently=False)