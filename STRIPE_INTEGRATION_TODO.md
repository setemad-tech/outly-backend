# Stripe integration — status & remaining steps

Outly takes payment with **hosted Stripe Checkout**: the customer is redirected
to a Stripe-hosted page, pays, and comes back. All money lands in Outly's own
Stripe account; organizers are paid out by Outly outside Stripe (no Stripe
Connect).

## Values to Replace

None. The Checkout Session call has no placeholder values — `mode`,
`success_url`, `cancel_url` and `line_items` are all real:

| Field | Current value | Notes |
|-------|---------------|-------|
| mode | `payment` | Tickets are one-time purchases. |
| success_url | `<frontend>/tickets/pending?booking_id=<id>` | Sent by the frontend; the backend appends the booking id. |
| cancel_url | `<frontend>/?event=<event id>` | Sent by the frontend. |
| line_items | Inline `price_data` built from the event (name, price, entry requirements) × ticket quantity | Intentionally **no** Stripe Price IDs — prices live on each Event, so no Products need to be created in the Dashboard. |

## Configured Parameters

Configured in Stripe's Checkout Studio and set in the Checkout Session call.

**Files containing these parameters:**
- [payments/services.py](payments/services.py) — `create_checkout_session()`

| Parameter | Value |
|-----------|-------|
| ui_mode | `hosted_page` (the API rejects the older `hosted` value) |
| billing_address_collection | `auto` |
| phone_number_collection | `{"enabled": false}` |
| automatic_tax | `{"enabled": false}` |
| allow_promotion_codes | `false` |
| submit_type | `auto` |
| integration_identifier | `hosted_web_0002` |
| origin_context | `web` |

`payment_method_collection` is not set: Stripe only uses it for subscriptions.

The call also keeps parameters Outly depends on, which are not part of Checkout
Studio and must not be removed:

| Parameter | Why |
|-----------|-----|
| `metadata.booking_id`, `client_reference_id` | How the webhook knows which booking was paid. |
| `expires_at` (31 min) | Stripe's minimum session lifetime; the actual spot hold is 5 min (`CHECKOUT_HOLD_MINUTES`). |
| `customer_email` | Prefills the buyer's email. |
| `payment_method_types: ["card"]` | Cards only, so every completed checkout is already paid. Delayed methods would need extra webhook handling. |
| idempotency key | A retried request can't create a second session for the same booking. |

## Setup

Environment variables (backend only; the frontend needs no Stripe key because
Checkout is hosted):

| Variable | Where | Value |
|----------|-------|-------|
| `STRIPE_SECRET_KEY` | Render → Environment (and `backend/.env` locally) | `sk_test_…` now, `sk_live_…` in production |
| `STRIPE_WEBHOOK_SECRET` | Render → Environment | Signing secret of the webhook endpoint below |

Webhook endpoint: `https://api.outly.ae/api/payments/webhook/`, listening for
`checkout.session.completed` and `checkout.session.expired`.

Dependency: `stripe` (Python) in `requirements.txt`.

## How it works

1. Customer picks a ticket quantity, ticks the Terms checkbox and clicks Book →
   `POST /api/bookings/<event_id>/checkout/` creates a PENDING booking (holding
   the spots for 5 minutes) and a Checkout Session, and returns its URL.
2. Customer pays on Stripe's page and is sent to `/tickets/pending`.
3. That page polls `POST /api/bookings/<id>/sync-payment/`, which asks Stripe
   whether the session is paid and confirms the booking. The
   `checkout.session.completed` webhook does the same independently —
   whichever arrives first wins.
4. Confirmed → ticket with QR shown on screen, emailed, and listed under
   Profile → My tickets.
5. Abandoned or expired checkouts are cancelled and their spots released.

Code: [payments/services.py](payments/services.py),
[payments/views.py](payments/views.py), [payments/urls.py](payments/urls.py).

## Testing (sandbox)

| Card | Result |
|------|--------|
| `4242 4242 4242 4242` | Succeeds |
| `4000 0025 0000 3155` | Requires 3-D Secure authentication |
| `4000 0000 0000 9995` | Declined (insufficient funds) |

Use any future expiry date, any CVC. Stripe's minimum charge in AED is 2.00,
so test events must cost more than that.

## Going live

1. **Activate the Stripe account** (Dashboard → activate payments): business
   details for OUTLY PORTAL L.L.C S.O.C, trade licence 1655330, bank account,
   owner ID. Until then the account can't take real payments.
2. If VAT-registered, add the TRN under Settings → Business → Tax details.
3. Create a **live** webhook endpoint for
   `https://api.outly.ae/api/payments/webhook/` with the same two events.
4. On Render, replace `STRIPE_SECRET_KEY` with the live `sk_live_…` key and
   `STRIPE_WEBHOOK_SECRET` with the live endpoint's `whsec_…`, then redeploy.
5. Make one small real purchase and refund it from the Dashboard.

## Next steps

- **Refunds** are handled manually by support (Terms §9) from the Stripe
  Dashboard → Payments → Refund. Paid tickets can't be self-cancelled in the app.
- **Organizer payouts** are done outside Stripe. Per-event revenue is in the
  organizer analytics (`GET /api/me/organizer/analytics/`).
- **Invoices/receipts for buyers** can be enabled with Checkout's
  `invoice_creation` if needed.

## Resources

- https://support.stripe.com
- https://docs.stripe.com/mcp
