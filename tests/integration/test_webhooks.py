"""Webhook intake + background processing.

Celery runs in eager mode in tests, so a queued event is processed before the HTTP call returns.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from app import tasks
from app.models import Payment, PaymentStatus, PaymentWebhookEvent, WebhookEventStatus
from app.services import payment_service
from tests.helpers import create_booking, pay, send_webhook, webhook_payload


@pytest.fixture
def pending_payment(client, catalogue, user_headers) -> dict:
    """A booking whose payment is waiting for the provider's webhook."""
    booking = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)
    payment = pay(client, user_headers, booking["id"], "DEFERRED").json()
    return {
        "booking_id": booking["id"],
        "payment_id": payment["id"],
        "provider_payment_id": payment["provider_payment_id"],
    }


def booking_status(client, headers, booking_id) -> str:
    return client.get(f"/bookings/{booking_id}", headers=headers).json()["status"]


def stored_event(db, provider_event_id) -> PaymentWebhookEvent:
    db.expire_all()
    return db.scalar(
        select(PaymentWebhookEvent).where(
            PaymentWebhookEvent.provider_event_id == provider_event_id
        )
    )


def count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def payment_status(db, payment_id) -> PaymentStatus:
    db.expire_all()
    return db.get(Payment, payment_id).status


# ---- Intake and processing ----


def test_success_webhook_is_accepted_then_confirms_booking(
    client, db, user_headers, pending_payment
):
    payload = webhook_payload(pending_payment["provider_payment_id"], "payment.succeeded")

    response = send_webhook(client, payload)

    assert response.status_code == 202
    assert response.json() == {"event_id": payload["event_id"], "result": "accepted"}
    event = stored_event(db, payload["event_id"])
    assert event.status == WebhookEventStatus.PROCESSED
    assert event.attempts == 1
    assert event.processed_at is not None
    assert booking_status(client, user_headers, pending_payment["booking_id"]) == "CONFIRMED"


def test_failed_webhook_fails_booking(client, db, user_headers, pending_payment):
    send_webhook(client, webhook_payload(pending_payment["provider_payment_id"], "payment.failed"))

    assert payment_status(db, pending_payment["payment_id"]) == PaymentStatus.FAILED
    assert booking_status(client, user_headers, pending_payment["booking_id"]) == "FAILED"


def test_same_event_twice_is_applied_once(client, db, user_headers, pending_payment):
    payload = webhook_payload(pending_payment["provider_payment_id"], "payment.succeeded")

    first = send_webhook(client, payload)
    second = send_webhook(client, payload)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json() == {"event_id": payload["event_id"], "result": "duplicate"}
    assert count(db, PaymentWebhookEvent) == 1
    assert count(db, Payment) == 1
    assert booking_status(client, user_headers, pending_payment["booking_id"]) == "CONFIRMED"


def test_replayed_event_id_cannot_smuggle_in_a_different_outcome(
    client, user_headers, pending_payment
):
    provider_payment_id = pending_payment["provider_payment_id"]
    send_webhook(client, webhook_payload(provider_payment_id, "payment.succeeded", "evt_fixed"))

    replay = send_webhook(
        client, webhook_payload(provider_payment_id, "payment.failed", "evt_fixed")
    )

    assert replay.json()["result"] == "duplicate"
    assert booking_status(client, user_headers, pending_payment["booking_id"]) == "CONFIRMED"


def test_concurrent_deliveries_of_same_event_are_stored_and_applied_once(
    client, db, pending_payment
):
    payload = webhook_payload(pending_payment["provider_payment_id"], "payment.succeeded")
    deliveries = 8
    start = threading.Barrier(deliveries)

    def deliver(_):
        start.wait()
        return send_webhook(client, payload).status_code

    with ThreadPoolExecutor(max_workers=deliveries) as pool:
        statuses = sorted(pool.map(deliver, range(deliveries)))

    assert statuses == [200] * (deliveries - 1) + [202]
    assert count(db, PaymentWebhookEvent) == 1
    assert stored_event(db, payload["event_id"]).status == WebhookEventStatus.PROCESSED
    assert payment_status(db, pending_payment["payment_id"]) == PaymentStatus.SUCCESS


def test_late_success_on_cancelled_booking_is_refunded(client, db, user_headers, pending_payment):
    client.post(f"/bookings/{pending_payment['booking_id']}/cancel", headers=user_headers)

    send_webhook(
        client, webhook_payload(pending_payment["provider_payment_id"], "payment.succeeded")
    )

    # The money was taken, the booking must not come back, so it is refunded automatically.
    assert payment_status(db, pending_payment["payment_id"]) == PaymentStatus.REFUNDED
    assert booking_status(client, user_headers, pending_payment["booking_id"]) == "CANCELLED"


def test_contradicting_event_for_final_payment_is_ignored(client, db, catalogue, user_headers):
    booking = create_booking(client, user_headers, catalogue.alpha.id, catalogue.cbc.id)
    payment = pay(client, user_headers, booking["id"], "SUCCESS").json()
    payload = webhook_payload(payment["provider_payment_id"], "payment.failed")

    response = send_webhook(client, payload)

    assert response.status_code == 202
    assert stored_event(db, payload["event_id"]).status == WebhookEventStatus.IGNORED
    assert payment_status(db, payment["id"]) == PaymentStatus.SUCCESS
    assert booking_status(client, user_headers, booking["id"]) == "CONFIRMED"


def test_unknown_provider_fields_are_accepted_and_kept(client, db, pending_payment):
    payload = {
        **webhook_payload(pending_payment["provider_payment_id"], "payment.succeeded"),
        "status": "SUCCESS",
        "livemode": False,
    }

    assert send_webhook(client, payload).status_code == 202
    assert stored_event(db, payload["event_id"]).payload["livemode"] is False


# ---- Rejected deliveries ----


def test_invalid_or_missing_signature_is_rejected(client, db, user_headers, pending_payment):
    payload = webhook_payload(pending_payment["provider_payment_id"], "payment.succeeded")

    forged = send_webhook(client, payload, signature="0" * 64)
    unsigned = client.post("/payments/webhook/", json=payload)

    assert forged.status_code == unsigned.status_code == 401
    assert count(db, PaymentWebhookEvent) == 0
    assert booking_status(client, user_headers, pending_payment["booking_id"]) == "PENDING"


def test_old_signed_webhook_is_rejected_as_a_replay(client, db, pending_payment):
    payload = webhook_payload(pending_payment["provider_payment_id"], "payment.succeeded")

    response = send_webhook(client, payload, timestamp=int(time.time()) - 3600)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_SIGNATURE"
    assert count(db, PaymentWebhookEvent) == 0


def test_unknown_payment_is_not_found_and_not_recorded(client, db):
    response = send_webhook(client, webhook_payload("pay_does_not_exist", "payment.succeeded"))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PAYMENT_NOT_FOUND"
    assert count(db, PaymentWebhookEvent) == 0


def test_malformed_webhook_is_rejected(client, pending_payment):
    payload = webhook_payload(pending_payment["provider_payment_id"], "payment.refunded")

    assert send_webhook(client, payload).status_code == 422


# ---- Retry handling ----


def _transient_error() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("server closed the connection"))


def test_transient_failures_are_retried_until_processing_succeeds(
    client, db, user_headers, pending_payment, monkeypatch
):
    real_process = payment_service.process_webhook_event
    calls = {"n": 0}

    def flaky(session, event_id):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _transient_error()
        return real_process(session, event_id)

    monkeypatch.setattr(payment_service, "process_webhook_event", flaky)
    payload = webhook_payload(pending_payment["provider_payment_id"], "payment.succeeded")

    send_webhook(client, payload)

    event = stored_event(db, payload["event_id"])
    assert calls["n"] == 3
    assert event.status == WebhookEventStatus.PROCESSED
    assert event.attempts == 3  # two recorded failures + the successful attempt
    assert event.last_error is None
    assert booking_status(client, user_headers, pending_payment["booking_id"]) == "CONFIRMED"


def test_event_is_marked_failed_after_exhausting_retries(
    client, db, user_headers, pending_payment, monkeypatch
):
    def always_down(session, event_id):
        raise _transient_error()

    monkeypatch.setattr(payment_service, "process_webhook_event", always_down)
    payload = webhook_payload(pending_payment["provider_payment_id"], "payment.succeeded")

    response = send_webhook(client, payload)

    assert response.status_code == 202  # stored safely; processing is the worker's problem
    event = stored_event(db, payload["event_id"])
    assert event.status == WebhookEventStatus.FAILED
    assert event.attempts == tasks.WEBHOOK_MAX_RETRIES + 1
    assert "server closed the connection" in event.last_error
    assert booking_status(client, user_headers, pending_payment["booking_id"]) == "PENDING"


def test_unexpected_errors_are_not_retried(client, db, pending_payment, monkeypatch):
    calls = {"n": 0}

    def buggy(session, event_id):
        calls["n"] += 1
        raise KeyError("bug")

    monkeypatch.setattr(payment_service, "process_webhook_event", buggy)
    payload = webhook_payload(pending_payment["provider_payment_id"], "payment.succeeded")

    send_webhook(client, payload)

    assert calls["n"] == 1
    assert stored_event(db, payload["event_id"]).status == WebhookEventStatus.FAILED


def test_processing_the_same_event_twice_is_harmless(client, db, pending_payment):
    payload = webhook_payload(pending_payment["provider_payment_id"], "payment.succeeded")
    send_webhook(client, payload)
    event = stored_event(db, payload["event_id"])

    # Celery may deliver a task more than once (acks_late), or the sweeper may re-enqueue it.
    tasks.process_webhook_event.delay(str(event.id))

    event = stored_event(db, payload["event_id"])
    assert event.status == WebhookEventStatus.PROCESSED
    assert event.attempts == 1


def test_sweeper_requeues_events_stuck_in_received(
    client, db, user_headers, pending_payment, monkeypatch
):
    # Simulate the broker being down at intake: the event is stored but never queued.
    monkeypatch.setattr("app.api.routes.payments.enqueue_webhook_processing", lambda event_id: None)
    payload = webhook_payload(pending_payment["provider_payment_id"], "payment.succeeded")
    send_webhook(client, payload)
    db.execute(
        update(PaymentWebhookEvent).values(received_at=datetime.now(UTC) - timedelta(hours=1))
    )
    db.commit()
    monkeypatch.undo()

    requeued = tasks.requeue_stuck_webhook_events.delay().get()

    assert requeued == 1
    assert stored_event(db, payload["event_id"]).status == WebhookEventStatus.PROCESSED
    assert booking_status(client, user_headers, pending_payment["booking_id"]) == "CONFIRMED"


def test_sweeper_leaves_recent_events_alone(db, client, pending_payment, monkeypatch):
    monkeypatch.setattr("app.api.routes.payments.enqueue_webhook_processing", lambda event_id: None)
    send_webhook(
        client, webhook_payload(pending_payment["provider_payment_id"], "payment.succeeded")
    )

    stuck = payment_service.find_stuck_webhook_events(db, older_than=tasks.STUCK_EVENT_AGE)

    assert stuck == []


# ---- Admin inbox operations ----


@pytest.fixture
def failed_event(client, db, pending_payment, monkeypatch) -> PaymentWebhookEvent:
    def buggy(session, event_id):
        raise KeyError("bug")

    monkeypatch.setattr(payment_service, "process_webhook_event", buggy)
    payload = webhook_payload(pending_payment["provider_payment_id"], "payment.succeeded")
    send_webhook(client, payload)
    monkeypatch.undo()
    return stored_event(db, payload["event_id"])


def test_admin_can_list_failed_events_and_replay_them(
    client, db, admin_headers, user_headers, pending_payment, failed_event
):
    listed = client.get(
        "/payments/webhook-events", params={"status_filter": "FAILED"}, headers=admin_headers
    )
    assert [e["id"] for e in listed.json()["items"]] == [str(failed_event.id)]

    retried = client.post(
        f"/payments/webhook-events/{failed_event.id}/retry", headers=admin_headers
    )

    assert retried.status_code == 202
    assert stored_event(db, failed_event.provider_event_id).status == WebhookEventStatus.PROCESSED
    assert booking_status(client, user_headers, pending_payment["booking_id"]) == "CONFIRMED"


def test_only_failed_events_can_be_replayed(client, db, admin_headers, pending_payment):
    payload = webhook_payload(pending_payment["provider_payment_id"], "payment.succeeded")
    send_webhook(client, payload)
    event = stored_event(db, payload["event_id"])

    response = client.post(f"/payments/webhook-events/{event.id}/retry", headers=admin_headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WEBHOOK_EVENT_NOT_FAILED"


def test_regular_users_cannot_see_the_webhook_inbox(client, user_headers):
    assert client.get("/payments/webhook-events", headers=user_headers).status_code == 403
