"""
bookings/qr.py
-----------------
One place that turns a signed payload into a QR PNG — used by both
emails.py (linking to it) and views.py (actually serving it).
"""

import io
import qrcode


def generate_qr_png(payload: str) -> bytes:
    img = qrcode.make(payload)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()