import json
import time
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from httpx2 import Response

from app.core.security import create_access_token, sign_webhook_payload
from app.models import User


def auth_headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def future_time(days: int = 3) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def create_booking(
    client: TestClient, headers: dict, centre_id: uuid.UUID, test_id: uuid.UUID
) -> dict:
    body = {"centre_id": str(centre_id), "test_id": str(test_id), "appointment_at": future_time()}
    response = client.post("/bookings", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def pay(client: TestClient, headers: dict, booking_id: str, outcome: str) -> Response:
    return client.post(
        "/payments/", json={"booking_id": booking_id, "outcome": outcome}, headers=headers
    )


def send_webhook(
    client: TestClient,
    payload: dict,
    *,
    signature: str | None = None,
    timestamp: int | None = None,
) -> Response:
    body = json.dumps(payload).encode()
    timestamp = int(time.time()) if timestamp is None else timestamp
    return client.post(
        "/payments/webhook/",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": str(timestamp),
            "X-Webhook-Signature": signature or sign_webhook_payload(body, timestamp),
        },
    )


def webhook_payload(provider_payment_id: str, event_type: str, event_id: str | None = None) -> dict:
    return {
        "event_id": event_id or f"evt_{uuid.uuid4().hex}",
        "event_type": event_type,
        "payment_id": provider_payment_id,
    }
