"""
chat/middleware.py
-------------------
Browsers can't set custom Authorization headers on a WebSocket handshake,
so the usual DRF token/JWT auth (which reads a header) doesn't apply here.
The standard workaround: pass the token as a query parameter on the ws://
URL, and validate it in ASGI middleware before the consumer ever runs.

Token-in-URL does mean the token can end up in server access logs — worth
knowing. Mitigations: use short-lived tokens for the WS handshake
specifically (rather than your long-lived API access token), and make sure
your ASGI server's access logs aren't retained/exposed carelessly.
"""

from urllib.parse import parse_qs
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from rest_framework_simplejwt.tokens import AccessToken  # if using SimpleJWT


@database_sync_to_async
def get_user_from_token(token: str):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        validated = AccessToken(token)
        return User.objects.get(pk=validated["user_id"])
    except Exception:
        return AnonymousUser()


class TokenAuthMiddleware:
    """ASGI middleware — wraps the Channels routing, not a Django middleware."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        query_string = parse_qs(scope["query_string"].decode())
        token = query_string.get("token", [None])[0]
        scope["user"] = await get_user_from_token(token) if token else AnonymousUser()
        return await self.app(scope, receive, send)