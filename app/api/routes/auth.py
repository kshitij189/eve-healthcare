from fastapi import APIRouter, Depends, Response, status

from app.api.dependencies import CurrentUser, DbSession, rate_limit_by_ip
from app.models import User
from app.schemas.auth import LoginRequest, RefreshRequest, SignupRequest, TokenResponse, UserOut
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])

# Per-IP limit on the unauthenticated endpoints, mainly against password guessing.
_auth_rate_limit = [Depends(rate_limit_by_ip("auth"))]


@router.post(
    "/signup",
    status_code=status.HTTP_201_CREATED,
    response_model=UserOut,
    dependencies=_auth_rate_limit,
)
def signup(data: SignupRequest, db: DbSession) -> User:
    """Create a patient account. Accounts created here always get the USER role."""
    return auth_service.signup(db, data)


@router.post("/login", response_model=TokenResponse, dependencies=_auth_rate_limit)
def login(data: LoginRequest, db: DbSession) -> TokenResponse:
    """Returns a short-lived access token and a single-use refresh token."""
    return auth_service.login(db, data.email, data.password)


@router.post("/refresh", response_model=TokenResponse, dependencies=_auth_rate_limit)
def refresh(data: RefreshRequest, db: DbSession) -> TokenResponse:
    """Swap a refresh token for a new token pair. The old refresh token stops working."""
    return auth_service.refresh(db, data.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, dependencies=_auth_rate_limit)
def logout(data: RefreshRequest, db: DbSession) -> Response:
    """Revoke a refresh token. Access tokens simply expire (15 minutes by default)."""
    auth_service.logout(db, data.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser) -> User:
    return user
