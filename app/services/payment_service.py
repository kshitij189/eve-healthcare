import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.core.logging import log_event
from app.models import (
    Booking,
    BookingStatus,
    Payment,
    PaymentStatus,
    PaymentWebhookEvent,
    User,
    WebhookEventStatus,
)
from app.schemas.payment import (
    EVENT_TYPE_TO_STATUS,
    PaymentCreate,
    SimulatedOutcome,
    WebhookEvent,
    WebhookResult,
)
from app.services import payment_provider
from app.services.booking_service import get_user_booking, is_past_payment_window

logger = logging.getLogger(__name__)


# ---- Simulated payment ----


def create_payment(db: Session, user: User, data: PaymentCreate) -> Payment:
    # Locking the booking serialises this with cancellation, expiry and webhooks for it.
    booking = get_user_booking(db, user, data.booking_id, for_update=True)
    if booking.status != BookingStatus.PENDING:
        raise ConflictError(
            f"Booking is {booking.status} and cannot be paid", code="BOOKING_NOT_PAYABLE"
        )
    if booking.payment is not None:
        raise ConflictError(
            "A payment for this booking is already in progress", code="PAYMENT_ALREADY_EXISTS"
        )
    if is_past_payment_window(booking, datetime.now(UTC)):
        # The expiry job may not have run yet; the rule applies regardless.
        raise ConflictError(
            "The payment window for this booking has closed", code="BOOKING_EXPIRED"
        )

    payment = Payment(
        booking=booking,
        provider_payment_id=payment_provider.new_provider_payment_id(),
        amount=booking.amount,
        status=PaymentStatus.PENDING,
    )
    db.add(payment)
    if data.outcome != SimulatedOutcome.DEFERRED:
        # Stands in for a synchronous call to the provider.
        _settle_payment(payment, PaymentStatus(data.outcome))

    try:
        db.commit()  # payment row and booking status are committed together, or not at all
    except IntegrityError:
        # Backstop for the unique booking_id constraint; the row lock should prevent this.
        db.rollback()
        raise ConflictError(
            "A payment for this booking already exists", code="PAYMENT_ALREADY_EXISTS"
        ) from None

    log_event(
        logger,
        "payment.created",
        payment_id=payment.id,
        booking_id=booking.id,
        outcome=data.outcome,
        payment_status=payment.status,
    )
    return payment


def _settle_payment(payment: Payment, result: PaymentStatus) -> None:
    """Record the provider's final answer and move the booking accordingly, if it still can."""
    payment.transition_to(result)
    booking = payment.booking
    target = BookingStatus.CONFIRMED if result == PaymentStatus.SUCCESS else BookingStatus.FAILED

    if booking.can_transition_to(target):
        booking.transition_to(target)
    elif result == PaymentStatus.SUCCESS:
        # The booking was cancelled or expired while the payment was in flight. The payment
        # keeps the provider's truth, the booking is not revived, and the money goes back.
        payment_provider.refund(payment, reason=f"booking_{booking.status.lower()}")


# ---- Webhook intake (HTTP request) ----


def receive_webhook(db: Session, event: WebhookEvent) -> tuple[WebhookResult, uuid.UUID | None]:
    """Store the event in the inbox exactly once. Processing happens in a background task.

    The unique constraint on provider_event_id decides which delivery "owns" an event, even for
    concurrent deliveries: Postgres makes the second INSERT wait for the first transaction and
    then insert nothing. Returns the stored event's id, or None for a duplicate.
    """
    payment_id = db.scalar(
        select(Payment.id).where(Payment.provider_payment_id == event.payment_id)
    )
    if payment_id is None:
        # Not stored, so if this was a provider-side race, its retry will still be accepted.
        raise NotFoundError("Unknown payment", code="PAYMENT_NOT_FOUND")

    stored_id = db.scalar(
        insert(PaymentWebhookEvent)
        .values(
            provider_event_id=event.event_id,
            payment_id=payment_id,
            event_type=event.event_type,
            payload=event.model_dump(mode="json"),
            status=WebhookEventStatus.RECEIVED,
        )
        .on_conflict_do_nothing(index_elements=[PaymentWebhookEvent.provider_event_id])
        .returning(PaymentWebhookEvent.id)
    )
    db.commit()

    if stored_id is None:
        log_event(logger, "webhook.duplicate", provider_event_id=event.event_id)
        return WebhookResult.DUPLICATE, None
    log_event(
        logger, "webhook.received", provider_event_id=event.event_id, webhook_event_id=stored_id
    )
    return WebhookResult.ACCEPTED, stored_id


# ---- Webhook processing (Celery worker) ----


def process_webhook_event(db: Session, event_id: uuid.UUID) -> WebhookEventStatus | None:
    """Apply a stored event to its payment and booking. Safe to run more than once.

    Celery delivers tasks at least once, and the sweeper may re-enqueue an event, so the event
    row is locked and its status checked first: anything not RECEIVED was already handled.
    """
    event = db.scalar(
        select(PaymentWebhookEvent).where(PaymentWebhookEvent.id == event_id).with_for_update()
    )
    if event is None:
        log_event(logger, "webhook.missing_event", logging.WARNING, webhook_event_id=event_id)
        return None
    if event.status != WebhookEventStatus.RECEIVED:
        db.rollback()
        return event.status

    # Same booking lock as cancel / pay / expiry.
    booking = db.scalar(
        select(Booking)
        .join(Booking.payment)
        .where(Payment.id == event.payment_id)
        .with_for_update(of=Booking)
    )
    payment = booking.payment
    reported = EVENT_TYPE_TO_STATUS[event.event_type]

    if payment.status == PaymentStatus.PENDING:
        _settle_payment(payment, reported)
        event.status = WebhookEventStatus.PROCESSED
    else:
        # A payment's outcome is final. A matching event is a late confirmation; a
        # contradicting one is recorded and flagged, but never flips the payment or booking.
        event.status = WebhookEventStatus.IGNORED
        contradicts = (payment.status == PaymentStatus.FAILED) != (reported == PaymentStatus.FAILED)
        if contradicts:
            log_event(
                logger,
                "webhook.conflicting_status",
                logging.WARNING,
                provider_event_id=event.provider_event_id,
                payment_status=payment.status,
                reported_status=reported,
            )

    event.attempts += 1
    event.last_error = None
    event.processed_at = datetime.now(UTC)
    db.commit()

    log_event(
        logger,
        f"webhook.{event.status.lower()}",
        provider_event_id=event.provider_event_id,
        payment_id=payment.id,
        payment_status=payment.status,
        booking_status=booking.status,
    )
    return event.status


def record_webhook_failure(db: Session, event_id: uuid.UUID, error: str, *, give_up: bool) -> None:
    event = db.scalar(
        select(PaymentWebhookEvent).where(PaymentWebhookEvent.id == event_id).with_for_update()
    )
    if event is None or event.status != WebhookEventStatus.RECEIVED:
        db.rollback()
        return
    event.attempts += 1
    event.last_error = error[:2000]
    if give_up:
        event.status = WebhookEventStatus.FAILED
    db.commit()
    log_event(
        logger,
        "webhook.failed" if give_up else "webhook.retry_scheduled",
        logging.ERROR if give_up else logging.WARNING,
        provider_event_id=event.provider_event_id,
        attempts=event.attempts,
        error=event.last_error,
    )


def find_stuck_webhook_events(
    db: Session, older_than: timedelta, limit: int = 100
) -> list[uuid.UUID]:
    """Events still RECEIVED long after arrival, e.g. because enqueueing failed after commit."""
    cutoff = datetime.now(UTC) - older_than
    return list(
        db.scalars(
            select(PaymentWebhookEvent.id)
            .where(
                PaymentWebhookEvent.status == WebhookEventStatus.RECEIVED,
                PaymentWebhookEvent.received_at < cutoff,
            )
            .order_by(PaymentWebhookEvent.received_at)
            .limit(limit)
        ).all()
    )


# ---- Admin operations on the inbox ----


def list_webhook_events(
    db: Session, status: WebhookEventStatus | None, *, limit: int, offset: int
) -> tuple[list[PaymentWebhookEvent], int]:
    query = select(PaymentWebhookEvent)
    if status is not None:
        query = query.where(PaymentWebhookEvent.status == status)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    events = db.scalars(
        query.order_by(PaymentWebhookEvent.received_at.desc()).limit(limit).offset(offset)
    ).all()
    return list(events), total


def reset_failed_webhook_event(db: Session, event_id: uuid.UUID) -> PaymentWebhookEvent:
    event = db.scalar(
        select(PaymentWebhookEvent).where(PaymentWebhookEvent.id == event_id).with_for_update()
    )
    if event is None:
        raise NotFoundError("Webhook event not found", code="WEBHOOK_EVENT_NOT_FOUND")
    if event.status != WebhookEventStatus.FAILED:
        raise ConflictError(
            f"Only FAILED events can be retried (this one is {event.status})",
            code="WEBHOOK_EVENT_NOT_FAILED",
        )
    event.status = WebhookEventStatus.RECEIVED
    db.commit()
    log_event(logger, "webhook.manual_retry", provider_event_id=event.provider_event_id)
    return event
