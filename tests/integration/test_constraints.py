"""The database, not only application code, enforces the invariants that matter."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models import (
    Booking,
    CentreTest,
    Payment,
    PaymentStatus,
    PaymentWebhookEvent,
    User,
)


@pytest.fixture
def payment(db, catalogue, make_user) -> Payment:
    user = make_user()
    offering = catalogue.alpha.offerings[0]
    booking = Booking(
        user=user,
        centre_test=offering,
        appointment_at=datetime.now(UTC) + timedelta(days=1),
        amount=offering.price,
    )
    payment = Payment(
        booking=booking,
        provider_payment_id="pay_1",
        amount=booking.amount,
        status=PaymentStatus.PENDING,
    )
    db.add(payment)
    db.commit()
    return payment


def test_duplicate_webhook_event_id_is_rejected(db, payment):
    db.add(
        PaymentWebhookEvent(
            provider_event_id="evt_1",
            payment_id=payment.id,
            event_type="payment.succeeded",
            payload={},
        )
    )
    db.commit()

    db.add(
        PaymentWebhookEvent(
            provider_event_id="evt_1",
            payment_id=payment.id,
            event_type="payment.succeeded",
            payload={},
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_duplicate_user_email_is_rejected(db):
    db.add(User(name="A", email="same@example.com", password_hash="x"))
    db.commit()

    db.add(User(name="B", email="same@example.com", password_hash="x"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_centre_cannot_offer_the_same_test_twice(db, catalogue):
    db.add(CentreTest(centre_id=catalogue.alpha.id, test_id=catalogue.cbc.id, price=Decimal("1")))

    with pytest.raises(IntegrityError):
        db.commit()


def test_booking_cannot_have_two_payments(db, payment):
    db.add(
        Payment(
            booking_id=payment.booking_id,
            provider_payment_id="pay_2",
            amount=payment.amount,
            status=PaymentStatus.PENDING,
        )
    )

    with pytest.raises(IntegrityError):
        db.commit()


def test_booking_status_is_constrained_in_the_database(db, payment):
    # Raw SQL on purpose: bypass the ORM enum to prove the CHECK constraint holds on its own.
    with pytest.raises(IntegrityError):
        db.execute(
            text("UPDATE bookings SET status = 'REFUNDED' WHERE id = :id"),
            {"id": payment.booking_id},
        )
