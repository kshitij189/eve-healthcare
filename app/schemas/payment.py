import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import BookingStatus, Payment, PaymentStatus, WebhookEventStatus
from app.schemas.common import RequestModel


class SimulatedOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    # The provider hasn't answered yet: the payment stays PENDING until a webhook arrives.
    DEFERRED = "DEFERRED"


class PaymentCreate(RequestModel):
    booking_id: uuid.UUID
    # Explicit rather than random so behaviour is reproducible in tests and demos.
    outcome: SimulatedOutcome


class PaymentOut(BaseModel):
    id: uuid.UUID
    booking_id: uuid.UUID
    provider_payment_id: str
    status: PaymentStatus
    amount: Decimal
    booking_status: BookingStatus
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, payment: Payment) -> "PaymentOut":
        return cls(
            id=payment.id,
            booking_id=payment.booking_id,
            provider_payment_id=payment.provider_payment_id,
            status=payment.status,
            amount=payment.amount,
            booking_status=payment.booking.status,
            created_at=payment.created_at,
            updated_at=payment.updated_at,
        )


WebhookEventType = Literal["payment.succeeded", "payment.failed"]

EVENT_TYPE_TO_STATUS: dict[str, PaymentStatus] = {
    "payment.succeeded": PaymentStatus.SUCCESS,
    "payment.failed": PaymentStatus.FAILED,
}


class WebhookEvent(BaseModel):
    # Unlike client requests, unknown fields are accepted (and kept in the stored payload):
    # providers add fields over time, and rejecting them would turn every delivery into an
    # endless retry. event_type is authoritative.
    model_config = ConfigDict(extra="allow")

    event_id: str = Field(min_length=1, max_length=255)
    event_type: WebhookEventType
    payment_id: str = Field(min_length=1, max_length=64, description="Provider payment id")


class WebhookResult(StrEnum):
    # New event, stored and queued for processing.
    ACCEPTED = "accepted"
    # Already-seen event id: acknowledged, nothing stored or queued.
    DUPLICATE = "duplicate"


class WebhookResponse(BaseModel):
    event_id: str
    result: WebhookResult


class WebhookEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider_event_id: str
    payment_id: uuid.UUID
    event_type: str
    status: WebhookEventStatus
    attempts: int
    last_error: str | None
    received_at: datetime
    processed_at: datetime | None
