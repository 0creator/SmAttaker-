"""
SmAttaker — Security Utilities
Encryption/decryption for exchange API keys, password hashing, JWT tokens.
"""
import base64
import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from cryptography.fernet import Fernet
from passlib.context import CryptContext
from jose import jwt, JWTError

from backend.config import settings

# ── Password Hashing ────────────────────────────────────
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── Encryption (for Exchange API keys) ──────────────────
def _get_fernet() -> Fernet:
    key = settings.ENCRYPTION_KEY
    if not key:
        raise ValueError("ENCRYPTION_KEY is not set in environment")
    return Fernet(key.encode())


def encrypt_api_key(plaintext: str) -> str:
    """Encrypt an exchange API key / secret for storage."""
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt_api_key(ciphertext: str) -> str:
    """Decrypt an exchange API key / secret."""
    f = _get_fernet()
    return f.decrypt(ciphertext.encode()).decode()


# ── JWT Tokens ──────────────────────────────────────────
def create_access_token(
    user_id: str,
    telegram_id: int,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """Create a JWT access token."""
    if expires_delta is None:
        expires_delta = timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "tid": telegram_id,
        "iat": now,
        "exp": now + expires_delta,
        "type": "access",
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(user_id: str, telegram_id: int) -> tuple[str, str]:
    """
    Create a JWT refresh token. Returns (token, jti).

    The `jti` (unique token id) is what makes rotation actually secure:
    the caller stores it as the user's *only* currently-valid refresh
    token id. When `/api/auth/refresh` is called, if the presented
    token's jti doesn't match what's on file, that token has already
    been rotated out (used before, or a stale/stolen copy) — treated as
    a security event rather than silently accepted.
    """
    import uuid
    jti = str(uuid.uuid4())
    expires_delta = timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS)
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "tid": telegram_id,
        "iat": now,
        "exp": now + expires_delta,
        "type": "refresh",
        "jti": jti,
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return token, jti


def decode_token(token: str) -> Optional[dict]:
    """Decode and validate a JWT token. Returns payload or None."""
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        return None


def verify_internal_api_key(provided: Optional[str]) -> bool:
    """
    Used to protect service-to-service endpoints (e.g. strategy engine ->
    signal ingestion) that no end-user should ever call directly.
    """
    if not settings.INTERNAL_API_KEY:
        # Fail closed: if no key is configured, refuse rather than silently allow.
        return False
    if not provided:
        return False
    return hmac.compare_digest(provided, settings.INTERNAL_API_KEY)


# ── Telegram Auth Hash Validator ────────────────────────
def validate_telegram_hash(data: dict, bot_token: str, max_age_seconds: int = 86400) -> bool:
    """
    Validate Telegram Login Widget hash per the official algorithm:
    https://core.telegram.org/widgets/login#checking-authorization

    secret_key = SHA256(bot_token)
    hash = HMAC_SHA256(check_string, key=secret_key)   ← must be HMAC, not a plain digest

    data: dict of all fields from Telegram (including 'hash' and 'auth_date')
    bot_token: your bot token
    max_age_seconds: reject stale login payloads (replay-attack protection)
    Returns True if valid.
    """
    data = dict(data)  # don't mutate the caller's dict
    received_hash = data.pop("hash", None)
    if not received_hash:
        return False

    auth_date = data.get("auth_date")
    if auth_date is not None:
        try:
            if time.time() - int(auth_date) > max_age_seconds:
                return False
        except (TypeError, ValueError):
            return False

    check_arr = [f"{k}={v}" for k, v in sorted(data.items()) if v is not None]
    check_string = "\n".join(check_arr)

    secret_key = hashlib.sha256(bot_token.encode()).digest()
    computed_hash = hmac.new(
        secret_key, check_string.encode(), hashlib.sha256
    ).hexdigest()
    # Constant-time comparison to avoid timing attacks
    return hmac.compare_digest(computed_hash, received_hash)


# ── NOWPayments IPN Signature Validator ─────────────────
def validate_nowpayments_ipn(raw_body: bytes, received_sig: str, ipn_secret: str) -> bool:
    """
    Validate a NOWPayments IPN callback using HMAC-SHA512 over the
    JSON payload with keys sorted alphabetically (NOWPayments spec).
    """
    import json

    if not received_sig or not ipn_secret:
        return False
    try:
        payload = json.loads(raw_body)
    except Exception:
        return False

    sorted_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    computed_sig = hmac.new(
        ipn_secret.encode(), sorted_payload.encode(), hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(computed_sig, received_sig)
