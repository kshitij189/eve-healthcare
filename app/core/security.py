import hashlib
import hmac
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.core.config import get_settings

_password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def create_access_token(user_id: uuid.UUID) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> uuid.UUID | None:
    """Return the user id from a valid token, or None if the token is invalid or expired."""
    settings = get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "exp"]},
        )
        return uuid.UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError):
        return None


def generate_refresh_token() -> str:
    # Opaque random string rather than a JWT: it is only ever checked against the database.
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    # A fast hash is fine for 384 bits of randomness; it just keeps raw tokens out of the DB.
    return hashlib.sha256(token.encode()).hexdigest()


def sign_webhook_payload(body: bytes, timestamp: int) -> str:
    # The timestamp is part of the signed message, so it can't be altered to dodge the age check.
    secret = get_settings().webhook_secret.encode()
    return hmac.new(secret, f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def is_valid_webhook_signature(
    body: bytes, timestamp: str, signature: str, now: float | None = None
) -> bool:
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    now = time.time() if now is None else now
    if abs(now - sent_at) > get_settings().webhook_tolerance_seconds:
        return False
    # compare_digest avoids leaking how many characters matched via response timing.
    return hmac.compare_digest(sign_webhook_payload(body, sent_at), signature)
