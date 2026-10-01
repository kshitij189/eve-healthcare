import uuid

from fastapi import APIRouter, Depends, Response, status

from app.api.dependencies import (
    AdminUser,
    CurrentUser,
    DbSession,
    PaginationParams,
    rate_limit_by_user,
    verify_webhook_signature,
)
from app.models import WebhookEventStatus
from app.schemas.common import Page
from app.schemas.payment import (
    PaymentCreate,
    PaymentOut,
    WebhookEvent,
    WebhookEventOut,
    WebhookResponse,
    WebhookResult,
)
from app.services import payment_service
from app.tasks import enqueue_webhook_processing

# Trailing slashes match the paths given in the assignment.
router = APIRouter(prefix="/payments", tags=["payments"])


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    response_model=PaymentOut,
    dependencies=[Depends(rate_limit_by_user("payments"))],
)
def create_payment(data: PaymentCreate, db: DbSession, user: CurrentUser) -> PaymentOut:
    """Simulate paying for a PENDING booking.

    `outcome` decides what the fake provider answers: SUCCESS confirms the booking, FAILED
    fails it, DEFERRED leaves the payment PENDING until `POST /payments/webhook/` resolves it.
    A declined payment is still a created payment, so FAILED also returns 201.
    """
    return PaymentOut.from_model(payment_service.create_payment(db, user, data))


@router.post(
    "/webhook/",
    response_model=WebhookResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(verify_webhook_signature)],
)
def payment_webhook(event: WebhookEvent, db: DbSession, response: Response) -> WebhookResponse:
    """Payment status updates from the provider. Safe to deliver the same event repeatedly.

    Requires `X-Webhook-Timestamp` (unix seconds) and `X-Webhook-Signature`: hex
    HMAC-SHA256 of `"{timestamp}." + raw body` using WEBHOOK_SECRET.

    A new event is stored and queued (202); processing, with retries, happens in a worker.
    A repeated event id returns 200 so the provider stops retrying.
    """
    result, stored_id = payment_service.receive_webhook(db, event)
    if stored_id is not None:
        enqueue_webhook_processing(stored_id)
    if result == WebhookResult.DUPLICATE:
        response.status_code = status.HTTP_200_OK
    return WebhookResponse(event_id=event.event_id, result=result)


@router.get("/webhook-events", response_model=Page[WebhookEventOut])
def list_webhook_events(
    db: DbSession,
    _: AdminUser,
    page: PaginationParams,
    status_filter: WebhookEventStatus | None = None,
) -> Page[WebhookEventOut]:
    """Admin: inspect the webhook inbox, e.g. `?status_filter=FAILED`."""
    events, total = payment_service.list_webhook_events(
        db, status_filter, limit=page.limit, offset=page.offset
    )
    return Page(
        items=[WebhookEventOut.model_validate(e) for e in events],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post(
    "/webhook-events/{event_id}/retry",
    response_model=WebhookEventOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_webhook_event(event_id: uuid.UUID, db: DbSession, _: AdminUser) -> WebhookEventOut:
    """Admin: re-queue an event that exhausted its automatic retries."""
    event = payment_service.reset_failed_webhook_event(db, event_id)
    enqueue_webhook_processing(event.id)
    return WebhookEventOut.model_validate(event)
