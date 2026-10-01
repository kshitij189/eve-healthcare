from datetime import UTC, datetime, timedelta

from redis import Redis
from sqlalchemy import update

from app.core.config import get_settings
from app.models import RefreshToken, User, UserRole
from tests.helpers import auth_headers

SIGNUP = {"name": "Asha", "email": "asha@example.com", "password": "s3cure-pass"}


def test_signup_creates_regular_user(client, db):
    response = client.post("/auth/signup", json=SIGNUP)

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "asha@example.com"
    assert body["role"] == "USER"
    assert "password" not in body and "password_hash" not in body
    stored = db.get(User, body["id"])
    assert stored.password_hash != SIGNUP["password"]


def test_signup_with_duplicate_email_is_a_conflict(client):
    client.post("/auth/signup", json=SIGNUP)

    # Emails are normalised, so a different case is still the same account.
    response = client.post("/auth/signup", json={**SIGNUP, "email": "ASHA@example.com"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EMAIL_TAKEN"


def test_signup_cannot_choose_its_own_role(client):
    response = client.post("/auth/signup", json={**SIGNUP, "role": "ADMIN"})

    assert response.status_code == 422


def test_signup_validation_errors_are_reported_without_echoing_input(client):
    response = client.post(
        "/auth/signup", json={"name": "", "email": "not-an-email", "password": "short"}
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert {d["field"] for d in error["details"]} == {"body.name", "body.email", "body.password"}
    assert "short" not in response.text


def test_login_returns_token_usable_on_protected_endpoints(client):
    client.post("/auth/signup", json=SIGNUP)

    response = client.post(
        "/auth/login", json={"email": SIGNUP["email"], "password": SIGNUP["password"]}
    )

    assert response.status_code == 200
    token = response.json()["access_token"]
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == SIGNUP["email"]


def test_login_with_wrong_password_or_unknown_email_gives_same_error(client):
    client.post("/auth/signup", json=SIGNUP)

    wrong_password = client.post(
        "/auth/login", json={"email": SIGNUP["email"], "password": "wrong-password"}
    )
    unknown_email = client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": "wrong-password"}
    )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


def test_missing_token_is_rejected(client):
    response = client.get("/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_invalid_token_is_rejected(client):
    response = client.get("/auth/me", headers={"Authorization": "Bearer not-a-token"})

    assert response.status_code == 401


def _login(client) -> dict:
    client.post("/auth/signup", json=SIGNUP)
    response = client.post(
        "/auth/login", json={"email": SIGNUP["email"], "password": SIGNUP["password"]}
    )
    assert response.status_code == 200
    return response.json()


def test_refresh_rotates_the_token_pair(client):
    tokens = _login(client)

    refreshed = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})

    assert refreshed.status_code == 200
    new = refreshed.json()
    assert new["refresh_token"] != tokens["refresh_token"]
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {new['access_token']}"})
    assert me.status_code == 200


def test_reusing_a_refresh_token_revokes_all_sessions(client):
    tokens = _login(client)
    rotated = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).json()

    # Someone replays the old (already used) token, e.g. after stealing it.
    reuse = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})

    assert reuse.status_code == 401
    assert reuse.json()["error"]["code"] == "INVALID_REFRESH_TOKEN"
    # The legitimate client's newer token is revoked too, forcing a fresh login.
    after = client.post("/auth/refresh", json={"refresh_token": rotated["refresh_token"]})
    assert after.status_code == 401


def test_logout_revokes_the_refresh_token(client):
    tokens = _login(client)

    assert (
        client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]}).status_code
        == 204
    )
    refresh = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refresh.status_code == 401


def test_expired_or_unknown_refresh_token_is_rejected(client, db):
    tokens = _login(client)
    db.execute(update(RefreshToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    db.commit()

    expired = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    unknown = client.post("/auth/refresh", json={"refresh_token": "made-up"})

    assert expired.status_code == unknown.status_code == 401


def test_login_is_rate_limited_per_client(client):
    client.post("/auth/signup", json=SIGNUP)
    limit = get_settings().rate_limit_auth_per_minute
    bad_login = {"email": SIGNUP["email"], "password": "wrong-password"}

    # Signup above used one request from the same client.
    statuses = [client.post("/auth/login", json=bad_login).status_code for _ in range(limit)]

    assert statuses[: limit - 1] == [401] * (limit - 1)
    assert statuses[-1] == 429
    blocked = client.post("/auth/login", json=bad_login)
    assert blocked.json()["error"]["code"] == "RATE_LIMITED"
    assert 1 <= int(blocked.headers["Retry-After"]) <= 60


def test_rate_limiting_fails_open_when_redis_is_down(client, monkeypatch):
    broken = Redis.from_url("redis://localhost:1/0", socket_connect_timeout=0.2)
    monkeypatch.setattr("app.core.rate_limit.get_redis", lambda: broken)

    response = client.post("/auth/signup", json=SIGNUP)

    assert response.status_code == 201


def test_token_for_deleted_user_is_rejected(client, db, make_user):
    user = make_user(role=UserRole.USER)
    headers = auth_headers(user)
    db.delete(user)
    db.commit()

    assert client.get("/auth/me", headers=headers).status_code == 401
