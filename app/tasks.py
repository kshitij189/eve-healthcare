"""Celery app and background jobs.

Run a worker:  celery -A app.tasks worker --loglevel=INFO
Run the beat:  celery -A app.tasks beat --loglevel=INFO
"""

import logging
import uuid
from datetime import timedelta

from celery import Celery
from celery.signals import setup_logging
from kombu.exceptions import KombuError
from redis import RedisError
from sqlalchemy.exc import InterfaceError, OperationalError

from app.core.config import get_settings
from app.core.logging import configure_logging, log_event
from app.db import SessionLocal
from app.services import booking_service, payment_service

logger = logging.getLogger(__name__)
settings = get_settings()

celery_app = Celery("eve", broker=settings.celery_broker_url)
celery_app.conf.update(
    task_always_eager=settings.celery_task_always_eager,
    task_ignore_result=True,
    # Acknowledge only after the task finishes, so a worker crash re-delivers the task instead
    # of losing it. Tasks therefore run at least once and must be idempotent (they are).
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    timezone="UTC",
    beat_schedule={
        "expire-unpaid-bookings": {
            "task": "app.tasks.expire_unpaid_bookings",
            "schedule": 60.0,
        },
        "requeue-stuck-webhook-events": {
            "task": "app.tasks.requeue_stuck_webhook_events",
            "schedule": 300.0,
        },
    },
)


@setup_logging.connect
def _use_app_logging(**_kwargs) -> None:
    # Stops Celery replacing our handlers, so worker logs have the same JSON shape as the API's.
    configure_logging(settings.log_level, settings.log_format)


# Connection drops, deadlocks and serialisation failures: worth retrying. Anything else is
# treated as a bug or bad data, which retrying won't fix.
TRANSIENT_ERRORS = (OperationalError, InterfaceError)
WEBHOOK_MAX_RETRIES = 5
# Generous margin over the full retry schedule (10+20+40+80+160 s) before the sweeper steps in.
STUCK_EVENT_AGE = timedelta(minutes=15)


def webhook_retry_delay(retries: int) -> int:
    """Exponential backoff: 10s, 20s, 40s, 80s, 160s (capped at 5 minutes)."""
    return min(10 * 2**retries, 300)


@celery_app.task(bind=True, max_retries=WEBHOOK_MAX_RETRIES)
def process_webhook_event(self, event_id: str) -> None:
    event_uuid = uuid.UUID(event_id)
    try:
        with SessionLocal() as db:
            payment_service.process_webhook_event(db, event_uuid)
    except TRANSIENT_ERRORS as exc:
        give_up = self.request.retries >= self.max_retries
        _record_failure(event_uuid, exc, give_up=give_up)
        if not give_up:
            raise self.retry(exc=exc, countdown=webhook_retry_delay(self.request.retries)) from exc
    except Exception as exc:
        logger.exception("webhook.processing_error", extra={"fields": {"event_id": event_id}})
        _record_failure(event_uuid, exc, give_up=True)


def _record_failure(event_id: uuid.UUID, exc: Exception, *, give_up: bool) -> None:
    try:
        with SessionLocal() as db:
            payment_service.record_webhook_failure(
                db, event_id, f"{type(exc).__name__}: {exc}", give_up=give_up
            )
    except Exception:
        # The DB may be the thing that's down. The retry is still scheduled, and the
        # sweeper re-queues anything left in RECEIVED.
        logger.exception("webhook.failure_not_recorded", extra={"fields": {"event_id": event_id}})


def enqueue_webhook_processing(event_id: uuid.UUID) -> None:
    """Queue an event for processing. Never raises: the event is already safely stored."""
    try:
        process_webhook_event.delay(str(event_id))
    except (KombuError, RedisError, OSError) as exc:
        # Broker unavailable. The sweeper will pick the event up once it's back.
        log_event(
            logger, "webhook.enqueue_failed", logging.ERROR, event_id=event_id, error=str(exc)
        )


@celery_app.task
def requeue_stuck_webhook_events() -> int:
    with SessionLocal() as db:
        stuck = payment_service.find_stuck_webhook_events(db, older_than=STUCK_EVENT_AGE)
    for event_id in stuck:
        enqueue_webhook_processing(event_id)
    if stuck:
        log_event(logger, "webhook.requeued_stuck", logging.WARNING, count=len(stuck))
    return len(stuck)


@celery_app.task
def expire_unpaid_bookings() -> int:
    with SessionLocal() as db:
        return booking_service.expire_unpaid_bookings(db)
