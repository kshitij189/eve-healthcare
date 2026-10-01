import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import NoReturn

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError, UnprocessableError
from app.core.logging import log_event
from app.models import (
    Booking,
    BookingStatus,
    CentreTest,
    DiagnosticCentre,
    DiagnosticTest,
    Payment,
    PaymentStatus,
    User,
)
from app.schemas.booking import BookingCreate
from app.services import payment_provider

logger = logging.getLogger(__name__)


def create_booking(db: Session, user: User, data: BookingCreate) -> Booking:
    # Always the live DB price, never the catalogue cache.
    offering = db.scalar(
        select(CentreTest).where(
            CentreTest.centre_id == data.centre_id, CentreTest.test_id == data.test_id
        )
    )
    if offering is None:
        _raise_for_missing_offering(db, data.centre_id, data.test_id)

    booking = Booking(
        user=user,
        centre_test=offering,
        appointment_at=data.appointment_at,
        amount=offering.price,
        status=BookingStatus.PENDING,
    )
    db.add(booking)
    db.commit()
    log_event(
        logger,
        "booking.created",
        booking_id=booking.id,
        user_id=user.id,
        centre_test_id=offering.id,
        amount=booking.amount,
    )
    return booking


def _raise_for_missing_offering(db: Session, centre_id: uuid.UUID, test_id: uuid.UUID) -> NoReturn:
    # Tell the client which part of the request is wrong, not just "not found".
    if db.get(DiagnosticCentre, centre_id) is None:
        raise NotFoundError("Diagnostic centre not found", code="CENTRE_NOT_FOUND")
    if db.get(DiagnosticTest, test_id) is None:
        raise NotFoundError("Diagnostic test not found", code="TEST_NOT_FOUND")
    raise UnprocessableError(
        "This test is not offered at the selected centre", code="TEST_NOT_OFFERED"
    )


def get_user_booking(
    db: Session, user: User, booking_id: uuid.UUID, *, for_update: bool = False
) -> Booking:
    """Load a booking owned by `user`.

    Another user's booking is reported as not found (not 403) so ids can't be probed.
    With for_update=True the booking row is locked until commit; every status change
    (cancel, payment, webhook, expiry) takes this lock, so they can't interleave on one booking.
    """
    query = select(Booking).where(Booking.id == booking_id, Booking.user_id == user.id)
    if for_update:
        query = query.with_for_update(of=Booking)
    booking = db.scalar(query)
    if booking is None:
        raise NotFoundError("Booking not found", code="BOOKING_NOT_FOUND")
    return booking


def list_user_bookings(
    db: Session, user: User, *, limit: int, offset: int
) -> tuple[list[Booking], int]:
    total = db.scalar(select(func.count()).select_from(Booking).where(Booking.user_id == user.id))
    bookings = db.scalars(
        select(Booking)
        .where(Booking.user_id == user.id)
        .order_by(Booking.created_at.desc(), Booking.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return list(bookings), total or 0


def cancel_booking(db: Session, user: User, booking_id: uuid.UUID) -> Booking:
    booking = get_user_booking(db, user, booking_id, for_update=True)
    if booking.status == BookingStatus.CONFIRMED and booking.appointment_at <= datetime.now(UTC):
        raise ConflictError(
            "The appointment time has passed; it can no longer be cancelled",
            code="APPOINTMENT_PASSED",
        )
    booking.transition_to(BookingStatus.CANCELLED)

    payment = booking.payment
    if payment is not None and payment.status == PaymentStatus.SUCCESS:
        payment_provider.refund(payment, reason="booking_cancelled")
    # A still-PENDING payment is left alone: if it later succeeds, the webhook refunds it.

    db.commit()
    log_event(logger, "booking.cancelled", booking_id=booking.id, user_id=user.id)
    return booking


def is_past_payment_window(booking: Booking, now: datetime) -> bool:
    window = timedelta(minutes=get_settings().booking_payment_window_minutes)
    return booking.created_at <= now - window or booking.appointment_at <= now


def expire_unpaid_bookings(db: Session, now: datetime | None = None, batch_size: int = 500) -> int:
    """Expire PENDING bookings with no payment that are past their payment window.

    Run periodically by Celery beat. Bookings with a payment in flight are skipped: the
    webhook decides those. SKIP LOCKED means we never wait on a booking someone is paying for.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(minutes=get_settings().booking_payment_window_minutes)
    candidates = db.scalars(
        select(Booking)
        .outerjoin(Booking.payment)
        .where(
            Booking.status == BookingStatus.PENDING,
            Payment.id.is_(None),
            or_(Booking.created_at <= cutoff, Booking.appointment_at <= now),
        )
        .order_by(Booking.created_at)
        .limit(batch_size)
        .with_for_update(of=Booking, skip_locked=True)
    ).all()

    expired = 0
    for booking in candidates:
        # Re-check under the lock: a payment may have been committed after our query's snapshot.
        if booking.status == BookingStatus.PENDING and booking.payment is None:
            booking.transition_to(BookingStatus.EXPIRED)
            expired += 1
    db.commit()
    if expired:
        log_event(logger, "booking.expired_batch", count=expired)
    return expired
