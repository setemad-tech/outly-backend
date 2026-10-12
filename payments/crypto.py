"""
payments/crypto.py
--------------------
Crypto payments through Binance Pay — NOT LIVE YET.

The Binance merchant account doesn't exist yet, so this module only decides
whether crypto checkout is available (it isn't until BINANCE_PAY_API_KEY and
BINANCE_PAY_SECRET_KEY are set). The app already shows "Crypto" as a payment
option, marked "coming soon" while is_configured() is False.

To switch it on once the merchant account exists:
  1. Set BINANCE_PAY_API_KEY / BINANCE_PAY_SECRET_KEY (Render env vars).
  2. Implement create_crypto_checkout(): POST /binancepay/openapi/v3/order
     with the booking id as merchantTradeNo, the event price as orderAmount,
     and returnUrl/cancelUrl like the Stripe flow; signed with HMAC-SHA512
     over "timestamp\\nnonce\\nbody\\n" (Binance Pay API docs). Save a
     Payment(provider=BINANCE_PAY, provider_reference=prepayId).
  3. Add a webhook view that verifies Binance's RSA signature (certificate
     from /binancepay/openapi/certificates) BEFORE confirming the booking —
     same rule as the Stripe webhook: never trust an unsigned callback.
  4. Release the held spot when the order expires, like handle_checkout_expired.
"""

from django.conf import settings


class CryptoPaymentsUnavailable(Exception):
    pass


def is_configured() -> bool:
    return bool(settings.BINANCE_PAY_API_KEY and settings.BINANCE_PAY_SECRET_KEY)


def create_crypto_checkout(*, booking, success_url: str, cancel_url: str):
    """Returns the Binance Pay checkout URL for a PENDING booking."""
    raise CryptoPaymentsUnavailable("Crypto payments aren't available yet — please pay by card.")
