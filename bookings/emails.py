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
so you can preview the real content without sending anything — staff can
open it at /api/admin/bookings/<id>/ticket-email/ (backoffice).

What was bought ("1 × Table for 6 · 6 guests" + the ticket's description)
sits right under the QR, so whoever scans or reads it sees the entitlement.
"""

from django.conf import settings
from django.utils.html import escape
from django.core.mail import EmailMultiAlternatives

from .models import Booking

_VIEW_SALT = "outly.ticket-view"


def ticket_view_token(booking: Booking) -> str:
    """Signed booking id for the "view your ticket" link (guests have no account to sign in to)."""
    from django.core import signing
    return signing.dumps(str(booking.id), salt=_VIEW_SALT)


def booking_from_view_token(token: str):
    """The booking a view link points at, or None if the link is bad."""
    from django.core import signing
    try:
        booking_id = signing.loads(token, salt=_VIEW_SALT)
    except signing.BadSignature:
        return None
    return Booking.objects.select_related("event", "user").prefetch_related("items").filter(pk=booking_id).first()


def ticket_view_url(booking: Booking) -> str:
    return f"{settings.FRONTEND_URL}/tickets/view/{ticket_view_token(booking)}"


def build_ticket_email_content(booking: Booking, *, qr_src: str | None = None) -> tuple[str, str, str]:
    """
    Returns (subject, text_body, html_body) — no sending, just content.
    qr_src overrides the hosted QR image (the preview inlines it instead).
    """
    event = booking.event
    reference = str(booking.id)[:8].upper()
    amount_display = f"{booking.price_paid_minor / 100:.2f} {event.currency}"
    when_display = event.start_at.strftime("%d %b %Y, %H:%M")

    qr_url = f"{settings.SITE_URL}/api/bookings/{booking.id}/qr.png/"
    view_url = ticket_view_url(booking)
    title = escape(event.title)

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

    # What was bought, line by line ("2 × VIP", "1 × Table for 6 · 6 guests"),
    # with the ticket's description ("includes a welcome drink") under it.
    items = list(booking.items.all())

    def item_line(i):
        return f"{i.quantity} × {i.name}" + (f" · {i.guests} guests" if i.guests_per_unit > 1 else "")

    items_html = (
        '<div style="margin: 16px 0 0; border: 2px solid #111; border-radius: 12px; padding: 12px 14px; text-align: left;">'
        '<p style="margin: 0 0 6px; font-size: 11px; letter-spacing: 2px; font-weight: 700; color: #666;">YOUR TICKETS</p>'
        + "".join(
            f'<p style="margin: 6px 0 0; font-size: 16px; font-weight: 800;">{escape(item_line(i))}</p>'
            + (f'<p style="margin: 2px 0 0; font-size: 13px; color: #444;">{escape(i.description)}</p>'
               if i.description else "")
            for i in items
        )
        + "</div>"
        if items else ""
    )
    items_text = "".join(
        f"  {item_line(i)}\n" + (f"    {i.description}\n" if i.description else "") for i in items
    )
    items_text = f"Your tickets:\n{items_text}" if items else ""
    promo_html = (
        f'<p style="margin: -14px 0 20px; font-size: 13px; color: #666;">Promo code '
        f"{escape(booking.promo_code_text)}: {booking.discount_minor // 100} AED off</p>"
        if booking.discount_minor else ""
    )
    promo_text = (
        f"Promo code {booking.promo_code_text}: {booking.discount_minor // 100} AED off\n"
        if booking.discount_minor else ""
    )

    used = booking.points_redeemed
    used_html = (
        f'<p style="margin: -14px 0 20px; font-size: 13px; color: #666;">{used} loyalty points used ({used} AED off)</p>'
        if used else ""
    )
    used_text = f"Loyalty points used: {used} ({used} AED off)\n" if used else ""

    points = booking.loyalty_points
    points_label = f"{points} loyalty point{'' if points == 1 else 's'}"
    points_html = (
        f'<p style="margin: 0 0 20px; font-size: 14px;">You earned <strong>{points_label}</strong></p>'
        if points else ""
    )
    points_text = f"You earned {points_label}\n" if points else ""

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
      <h2 style="margin: 8px 0 4px;">{title}</h2>
      <p style="color: #666; margin: 0 0 2px;">{escape(event.venue_name)}</p>
      <p style="color: #666; margin: 0 0 20px;">{when_display}</p>
      <p style="margin: 0 0 20px; font-size: 14px;">
        Payment confirmed: <strong>{amount_display}</strong>
      </p>
      {promo_html}
      {used_html}
      {points_html}
      {requirements_html}
      <img src="{qr_src or qr_url}" alt="Your ticket QR code" width="220" height="220" style="display:block;" />
      <p style="margin: 16px 0 0; font-size: 14px; font-weight: 600;">Ticket for</p>
      <p style="margin: 2px 0 0; font-size: 14px; color: #666;">{escape(holder_display)}</p>
      {admits_html}
      {items_html}
      <p style="margin: 20px 0 0;">
        <a href="{escape(view_url)}" style="display:inline-block; background:#713fe6; color:#fff; text-decoration:none; font-weight:700; font-size:14px; padding:10px 18px; border-radius:9999px;">View your ticket online</a>
      </p>
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
        f"{promo_text}"
        f"{used_text}"
        f"{points_text}"
        f"{requirements_text}"
        f"Ticket for: {holder_display}\n"
        f"{admits_text}"
        f"{items_text}"
        f"Booking reference: {reference}\n"
        f"QR code: {qr_url}\n"
        f"View your ticket online: {view_url}\n"
        "Show your QR ticket at the entrance."
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