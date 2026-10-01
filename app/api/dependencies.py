import logging
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Header, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AuthenticationError, PermissionDeniedError
from app.core.logging import log_event
from app.core.rate_limit import enforce_rate_limit
from app.core.security import decode_access_token, is_valid_webhook_signature
from app.db import get_db
from app.models import User, UserRole

logger = logging.getLogger(__name__)

DbSession = Annotated[Session, Depends(get_db)]

# auto_error=False so a missing header produces our 401 error format, not FastAPI's default.
_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    if credentials is None:
        raise AuthenticationError("Missing bearer token")
    user_id = decode_access_token(credentials.credentials)
    # Load the user on every request so deleted users lose access even with an unexpired token.
    user = db.get(User, user_id) if user_id else None
    if user is None:
        log_event(logger, "auth.invalid_token", logging.WARNING)
        raise AuthenticationError("Invalid or expired token")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    if user.role != UserRole.ADMIN:
        raise PermissionDeniedError("Admin access required")
    return user


AdminUser = Annotated[User, Depends(require_admin)]


def rate_limit_by_ip(scope: str) -> Callable[[Request], None]:
    def dependency(request: Request) -> None:
        # request.client is the direct peer. Behind a reverse proxy this would need the
        # proxy's X-Forwarded-For, configured via uvicorn's --forwarded-allow-ips.
        client_ip = request.client.host if request.client else "unknown"
        enforce_rate_limit(scope, client_ip, get_settings().rate_limit_auth_per_minute)

    return dependency


def rate_limit_by_user(scope: str) -> Callable[[User], None]:
    def dependency(user: CurrentUser) -> None:
        enforce_rate_limit(scope, str(user.id), get_settings().rate_limit_writes_per_minute)

    return dependency


async def verify_webhook_signature(
    request: Request,
    x_webhook_signature: Annotated[str | None, Header()] = None,
    x_webhook_timestamp: Annotated[str | None, Header()] = None,
) -> None:
    # Signed over the raw bytes: re-serialising parsed JSON wouldn't reproduce them exactly.
    body = await request.body()
    if not (
        x_webhook_signature
        and x_webhook_timestamp
        and is_valid_webhook_signature(body, x_webhook_timestamp, x_webhook_signature)
    ):
        log_event(logger, "webhook.invalid_signature", logging.WARNING)
        raise AuthenticationError(
            "Invalid, missing or expired webhook signature", code="INVALID_SIGNATURE"
        )


class Pagination:
    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> None:
        self.limit = limit
        self.offset = offset


PaginationParams = Annotated[Pagination, Depends()]
