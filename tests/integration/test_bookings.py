import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from app.models import Booking, BookingStatus, Payment, PaymentStatus
from app.tasks import expire_unpaid_bookings
from tests.helpers import create_booking, future_time, pay


def booking_request(centre_id, test_id, appointment_at=None) -> dict:
    return {
        "centre_id": str(centre_id),
        "test_id": str(test_id),
        "appointment_at": appointment_at or future_time(),
    }


def test_create_booking_uses_price_from_the_centre_offering(client, catalogue, user_headers):
    response = client.post(
        "/bookings", json=booking_request(catalogue.beta.id, catalogue.cbc.id), headers=user_headers
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "PENDING"
    assert body["amount"] == "450.00"
    assert body["centre"]["name"] == "Beta Diagnostics"
    assert body["test"]["name"] == "Complete Blood Count"
    assert body["patient"]["name"].startswith("User")


def test_client_cannot_submit_its_own_amount(client, catalogue, user_headers):
    response = client.post(
        "/bookings",
        json={**booking_request(catalogue.alpha.id, catalogue.cbc.id), "amount": "1.00"},
        headers=user_headers,
    )

    assert response.status_code == 422


def test_test_not_offered_at_centre_is_rejected(client, catalogue, user_headers):
    # Lipid profile exists, but Beta doesn't offer it.
    response = client.post(
        "/bookings",
        json=booking_request(catalogue.beta.id, catalogue.lipid.id),
        headers=user_headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TEST_NOT_OFFERED"


def test_nonexistent_centre_or_test_is_not_found(client, catalogue, user_headers):
    no_centre = client.post(
        "/bookings", json=booking_request(uuid.uuid4(), catalogue.cbc.id), headers=user_headers
    )
    no_test = client.post(
        "/bookings", json=booking_request(catalogue.alpha.id, uuid.uuid4()), headers=user_headers
    )

    assert no_centre.status_code == 404
    assert no_centre.json()["error"]["code"] == "CENTRE_NOT_FOUND"
    assert no_test.status_code == 404
    assert no_test.json()["error"]["code"] == "TEST_NOT_FOUND"


def test_appointment_must_be_in_the_future_and_timezone_aware(client, catalogue, user_headers):
    past = (datetime.now().astimezone() - timedelta(hours=1)).isoformat()
    naive = (datetime.now() + timedelta(days=1)).replace(microsecond=0).isoformat()

    for appointment_at in (past, naive):
        response = client.post(
            "/bookings",
            json=booking_request(catalogue.alpha.id, catalogue.cbc.id, appointment_at),
            headers=user_headers,
        )
        assert response.status_code == 422, appointment_at


def test_booking_requires_authentication(client, catalogue):
    response = client.post("/bookings", json=booking_request(catalogue.alpha.id, catalogue.cbc.id))

    assert response.status_code == 401


def test_users_only_see_their_own_bookings(client, catalogue, user_headers, other_user_headers):
    mine = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)
    create_booking(client, other_user_headers, catalogue.alpha.id, catalogue.lipid.id)

    listed = client.get("/bookings", headers=user_headers).json()

    assert listed["total"] == 1
    assert [b["id"] for b in listed["items"]] == [mine["id"]]


def test_user_cannot_access_or_cancel_another_users_booking(
    client, catalogue, user_headers, other_user_headers
):
    booking = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)

    read = client.get(f"/bookings/{booking['id']}", headers=other_user_headers)
    cancel = client.post(f"/bookings/{booking['id']}/cancel", headers=other_user_headers)

    # 404 rather than 403, so the response doesn't confirm the booking exists.
    assert read.status_code == cancel.status_code == 404
    owner_view = client.get(f"/bookings/{booking['id']}", headers=user_headers).json()
    assert owner_view["status"] == "PENDING"


def test_unknown_and_malformed_booking_ids(client, user_headers):
    assert client.get(f"/bookings/{uuid.uuid4()}", headers=user_headers).status_code == 404
    assert client.get("/bookings/12345", headers=user_headers).status_code == 422


def test_cancel_pending_booking(client, catalogue, user_headers):
    booking = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)

    response = client.post(f"/bookings/{booking['id']}/cancel", headers=user_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"


def test_cancelling_twice_is_an_invalid_transition(client, catalogue, user_headers):
    booking = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)
    client.post(f"/bookings/{booking['id']}/cancel", headers=user_headers)

    response = client.post(f"/bookings/{booking['id']}/cancel", headers=user_headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE_TRANSITION"


def test_cancelling_confirmed_booking_refunds_the_payment(client, db, catalogue, user_headers):
    booking = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)
    payment = pay(client, user_headers, booking["id"], "SUCCESS").json()

    response = client.post(f"/bookings/{booking['id']}/cancel", headers=user_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert db.get(Payment, payment["id"]).status == PaymentStatus.REFUNDED


def test_confirmed_booking_cannot_be_cancelled_after_appointment(
    client, db, catalogue, user_headers
):
    booking = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)
    payment = pay(client, user_headers, booking["id"], "SUCCESS").json()
    _set_booking(db, booking["id"], appointment_at=datetime.now(UTC) - timedelta(hours=1))

    response = client.post(f"/bookings/{booking['id']}/cancel", headers=user_headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "APPOINTMENT_PASSED"
    assert db.get(Payment, payment["id"]).status == PaymentStatus.SUCCESS


def test_failed_or_expired_booking_cannot_be_cancelled(client, db, catalogue, user_headers):
    failed = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)
    pay(client, user_headers, failed["id"], "FAILED")
    expired = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)
    _set_booking(db, expired["id"], status=BookingStatus.EXPIRED)

    for booking in (failed, expired):
        response = client.post(f"/bookings/{booking['id']}/cancel", headers=user_headers)
        assert response.status_code == 409


# ---- Expiry of unpaid bookings (Celery beat job) ----


def _set_booking(db, booking_id, **values) -> None:
    db.execute(update(Booking).where(Booking.id == booking_id).values(**values))
    db.commit()
    db.expire_all()


def test_unpaid_booking_expires_after_payment_window(client, db, catalogue, user_headers):
    stale = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)
    fresh = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)
    _set_booking(db, stale["id"], created_at=datetime.now(UTC) - timedelta(hours=1))

    expired = expire_unpaid_bookings.delay().get()

    assert expired == 1
    assert (
        client.get(f"/bookings/{stale['id']}", headers=user_headers).json()["status"] == "EXPIRED"
    )
    assert (
        client.get(f"/bookings/{fresh['id']}", headers=user_headers).json()["status"] == "PENDING"
    )


def test_unpaid_booking_expires_once_appointment_time_passes(client, db, catalogue, user_headers):
    booking = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)
    _set_booking(db, booking["id"], appointment_at=datetime.now(UTC) - timedelta(minutes=1))

    expire_unpaid_bookings.delay()

    assert (
        client.get(f"/bookings/{booking['id']}", headers=user_headers).json()["status"] == "EXPIRED"
    )


def test_booking_with_payment_in_flight_is_not_expired(client, db, catalogue, user_headers):
    booking = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)
    pay(client, user_headers, booking["id"], "DEFERRED")
    _set_booking(db, booking["id"], created_at=datetime.now(UTC) - timedelta(hours=1))

    assert expire_unpaid_bookings.delay().get() == 0
    assert (
        client.get(f"/bookings/{booking['id']}", headers=user_headers).json()["status"] == "PENDING"
    )


def test_price_change_does_not_affect_existing_booking(
    client, catalogue, user_headers, admin_headers
):
    booking = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)

    client.patch(
        f"/centres/{catalogue.alpha.id}/tests/{catalogue.cbc.id}",
        json={"price": "999.00"},
        headers=admin_headers,
    )

    refreshed = client.get(f"/bookings/{booking['id']}", headers=user_headers).json()
    assert refreshed["amount"] == "500.00"
