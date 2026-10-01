import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AuthenticationError, ConflictError
from app.core.logging import log_event
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.models import RefreshToken, User, UserRole
from app.schemas.auth import SignupRequest, TokenResponse

logger = logging.getLogger(__name__)


def signup(db: Session, data: SignupRequest) -> User:
    user = User(
        name=data.name,
        email=data.email,
        password_hash=hash_password(data.password),
        role=UserRole.USER,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # The unique constraint is the check, so two simultaneous signups can't both succeed.
        db.rollback()
        raise ConflictError("Email is already registered", code="EMAIL_TAKEN") from None
    log_event(logger, "auth.signup", user_id=user.id)
    return user


def login(db: Session, email: str, password: str) -> TokenResponse:
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(user.password_hash, password):
        log_event(logger, "auth.login_failed", logging.WARNING)
        # Same message for unknown email and wrong password: don't reveal which emails exist.
        raise AuthenticationError("Invalid email or password", code="INVALID_CREDENTIALS")
    tokens = _issue_tokens(db, user)
    db.commit()
    log_event(logger, "auth.login", user_id=user.id)
    return tokens


def refresh(db: Session, raw_token: str) -> TokenResponse:
    """Exchange a refresh token for a new pair. Each refresh token works exactly once.

    The token is claimed with a single conditional UPDATE, so two concurrent refreshes with the
    same token can't both succeed. Presenting an already-used token means it was copied
    (or a client is buggy), so every session of that user is revoked.
    """
    now = datetime.now(UTC)
    token_hash = hash_refresh_token(raw_token)
    user_id = db.scalar(
        update(RefreshToken)
        .where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now,
        )
        .values(revoked_at=now)
        .returning(RefreshToken.user_id)
    )
    if user_id is None:
        _revoke_all_if_reused(db, token_hash, now)
        raise AuthenticationError("Invalid or expired refresh token", code="INVALID_REFRESH_TOKEN")

    user = db.get(User, user_id)
    tokens = _issue_tokens(db, user)
    db.commit()
    log_event(logger, "auth.refresh", user_id=user_id)
    return tokens


def logout(db: Session, raw_token: str) -> None:
    # Idempotent: logging out with an unknown or already-revoked token is not an error.
    db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.token_hash == hash_refresh_token(raw_token),
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )
    db.commit()


def _issue_tokens(db: Session, user: User) -> TokenResponse:
    settings = get_settings()
    raw_refresh = generate_refresh_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_refresh_token(raw_refresh),
            expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days),
        )
    )
    return TokenResponse(
        access_token=create_access_token(user.id),
        refresh_token=raw_refresh,
        expires_in=settings.access_token_expire_minutes * 60,
    )


def _revoke_all_if_reused(db: Session, token_hash: str, now: datetime) -> None:
    used = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    if used is None or used.revoked_at is None:
        return  # unknown or merely expired
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == used.user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    db.commit()
    log_event(logger, "auth.refresh_token_reused", logging.WARNING, user_id=used.user_id)
