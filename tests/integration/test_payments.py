import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app.models import Booking, Payment
from tests.helpers import create_booking, pay


def booking_status(client, headers, booking_id) -> str:
    return client.get(f"/bookings/{booking_id}", headers=headers).json()["status"]


def test_successful_payment_confirms_booking(client, catalogue, user_headers):
    booking = create_booking(client, user_headers, catalogue.alpha.id, catalogue.lipid.id)

    response = pay(client, user_headers, booking["id"], "SUCCESS")

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "SUCCESS"
    assert body["amount"] == "800.00"
    assert body["provider_payment_id"].startswith("pay_")
    assert body["booking_status"] == "CONFIRMED"
    assert booking_status(client, user_headers, booking["id"]) == "CONFIRMED"


def test_failed_payment_fails_booking(client, catalogue, user_headers):
    booking = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)

    response = pay(client, user_headers, booking["id"], "FAILED")

    assert response.status_code == 201
    assert response.json()["status"] == "FAILED"
    assert booking_status(client, user_headers, booking["id"]) == "FAILED"


def test_deferred_payment_stays_pending(client, catalogue, user_headers):
    booking = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)

    response = pay(client, user_headers, booking["id"], "DEFERRED")

    assert response.json()["status"] == "PENDING"
    assert booking_status(client, user_headers, booking["id"]) == "PENDING"


def test_payment_for_unknown_booking_is_not_found(client, user_headers):
    response = pay(client, user_headers, str(uuid.uuid4()), "SUCCESS")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "BOOKING_NOT_FOUND"


def test_user_cannot_pay_for_another_users_booking(
    client, catalogue, user_headers, other_user_headers, db
):
    booking = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)

    response = pay(client, other_user_headers, booking["id"], "SUCCESS")

    assert response.status_code == 404
    assert booking_status(client, user_headers, booking["id"]) == "PENDING"
    assert db.scalar(select(func.count()).select_from(Payment)) == 0


def test_payment_requires_authentication(client, catalogue, user_headers):
    booking = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)

    response = client.post("/payments/", json={"booking_id": booking["id"], "outcome": "SUCCESS"})

    assert response.status_code == 401


def test_invalid_outcome_is_rejected(client, catalogue, user_headers):
    booking = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)

    response = pay(client, user_headers, booking["id"], "MAYBE")

    assert response.status_code == 422


def test_confirmed_booking_cannot_be_paid_again(client, catalogue, user_headers):
    booking = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)
    pay(client, user_headers, booking["id"], "SUCCESS")

    response = pay(client, user_headers, booking["id"], "SUCCESS")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "BOOKING_NOT_PAYABLE"


def test_failed_or_cancelled_booking_cannot_be_paid(client, catalogue, user_headers):
    failed = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)
    pay(client, user_headers, failed["id"], "FAILED")
    cancelled = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)
    client.post(f"/bookings/{cancelled['id']}/cancel", headers=user_headers)

    assert pay(client, user_headers, failed["id"], "SUCCESS").status_code == 409
    assert pay(client, user_headers, cancelled["id"], "SUCCESS").status_code == 409
    assert booking_status(client, user_headers, failed["id"]) == "FAILED"
    assert booking_status(client, user_headers, cancelled["id"]) == "CANCELLED"


def test_second_payment_while_one_is_pending_is_rejected(client, catalogue, user_headers):
    booking = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)
    pay(client, user_headers, booking["id"], "DEFERRED")

    response = pay(client, user_headers, booking["id"], "SUCCESS")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PAYMENT_ALREADY_EXISTS"


@pytest.mark.parametrize(
    "stale_field",
    [
        {"created_at": datetime.now(UTC) - timedelta(hours=1)},  # payment window closed
        {"appointment_at": datetime.now(UTC) - timedelta(minutes=1)},  # appointment passed
    ],
)
def test_booking_past_its_payment_window_cannot_be_paid(
    client, db, catalogue, user_headers, stale_field
):
    # The expiry job may not have run yet; payment must still be refused.
    booking = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)
    db.execute(update(Booking).where(Booking.id == booking["id"]).values(**stale_field))
    db.commit()

    response = pay(client, user_headers, booking["id"], "SUCCESS")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "BOOKING_EXPIRED"
    assert db.scalar(select(func.count()).select_from(Payment)) == 0


def test_concurrent_payment_requests_create_one_payment(client, catalogue, user_headers, db):
    booking = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)
    attempts = 5
    start = threading.Barrier(attempts)

    def attempt(_):
        start.wait()
        return pay(client, user_headers, booking["id"], "SUCCESS").status_code

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        statuses = sorted(pool.map(attempt, range(attempts)))

    assert statuses == [201] + [409] * (attempts - 1)
    assert db.scalar(select(func.count()).select_from(Payment)) == 1
