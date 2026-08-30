"""
SmAttaker — Mini App Authentication
=====================================
Tiny JWT-style token verification for the Telegram Mini App endpoint.

The signal_broadcast._build_miniapp_url_for_user() helper mints a token
of the form `<base64url(payload)>.<hmac_sha256_hex>` where payload is
JSON `{uid, tid, lang, sid, exp}`. This module's `verify_miniapp_token`
does the inverse — verifies the HMAC, decodes the payload, and returns
a MiniappAuthContext (or None on any failure).

We deliberately use a hand-rolled JWT-style scheme rather than pulling
in `PyJWT` — we don't need claims-format flexibility, RS256, JWE, etc.
A single 32-byte HMAC-SHA256 over a base64url JSON body is enough
security for a 24h validity, single-purpose token, and keeping it
hand-rolled means one fewer transitive dependency to track.
"""
import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class MiniappAuthContext:
    """The decoded payload of a valid Mini App auth token."""
    user_id: str
    telegram_id: str
    language: str
    signal_id: str
    expires_at: int  # unix epoch seconds


def verify_miniapp_token(token: str) -> Optional[MiniappAuthContext]:
    """Verify a Mini App auth token and return its decoded payload.

    Returns None on ANY of:
      - malformed token (not `<body>.<sig>`)
      - body is not valid base64url JSON
      - HMAC mismatch (signature doesn't match)
      - token expired (exp < now)

    The token is short-lived (24h by default) and bound to a specific
    signal_id — even if a token leaks, it can only be used to view the
    one signal it was minted for, and only for 24h.
    """
    if not token:
        return None

    parts = token.split(".")
    if len(parts) != 2:
        return None
    body_b64, sig_hex = parts
    if not body_b64 or not sig_hex:
        return None

    # Re-pad base64url (we stripped padding on mint, see _build_miniapp_url_for_user)
    pad = "=" * (-len(body_b64) % 4)
    try:
        body_bytes = base64.urlsafe_b64decode(body_b64 + pad)
        payload = json.loads(body_bytes.decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None

    # Verify HMAC
    from backend.config import settings
    secret = settings.SECRET_KEY.encode("utf-8")
    expected_sig = hmac.new(secret, body_b64.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, sig_hex):
        return None

    # Check expiry
    exp = payload.get("exp")
    if not isinstance(exp, int):
        return None
    if exp < int(time.time()):
        return None

    return MiniappAuthContext(
        user_id=str(payload.get("uid", "")),
        telegram_id=str(payload.get("tid", "")),
        language=str(payload.get("lang", "en")),
        signal_id=str(payload.get("sid", "")),
        expires_at=exp,
    )
