import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.errors import ConflictError
from app.db import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.booking import Booking


class PaymentStatus(StrEnum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


PAYMENT_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.PENDING: frozenset({PaymentStatus.SUCCESS, PaymentStatus.FAILED}),
    PaymentStatus.SUCCESS: frozenset({PaymentStatus.REFUNDED}),
    PaymentStatus.FAILED: frozenset(),
    PaymentStatus.REFUNDED: frozenset(),
}


class Payment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "payments"

    # Unique: a failed payment makes the booking FAILED (final), so a booking never needs a
    # second payment. This also stops a double-submitted POST /payments/ charging twice.
    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id", ondelete="RESTRICT"), unique=True
    )
    # The id the (simulated) provider knows this payment by; webhooks refer to it.
    provider_payment_id: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(
            PaymentStatus,
            name="payment_status",
            native_enum=False,
            create_constraint=True,
            length=20,
        ),
        default=PaymentStatus.PENDING,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))

    booking: Mapped[Booking] = relationship(back_populates="payment")

    def transition_to(self, new_status: PaymentStatus) -> None:
        if new_status not in PAYMENT_TRANSITIONS[self.status]:
            raise ConflictError(
                f"Payment cannot move from {self.status} to {new_status}",
                code="INVALID_PAYMENT_TRANSITION",
            )
        self.status = new_status


class WebhookEventStatus(StrEnum):
    RECEIVED = "RECEIVED"  # stored, waiting for (or between) processing attempts
    PROCESSED = "PROCESSED"  # applied to the payment/booking
    IGNORED = "IGNORED"  # valid, but the payment was already final
    FAILED = "FAILED"  # gave up after retries; needs an admin to look and replay


class PaymentWebhookEvent(UUIDPrimaryKeyMixin, Base):
    """Inbox of provider events; the unique event id makes delivery idempotent."""

    __tablename__ = "payment_webhook_events"
    # Lets the sweeper find stuck RECEIVED events without scanning the table.
    __table_args__ = (
        Index("ix_payment_webhook_events_status_received_at", "status", "received_at"),
    )

    provider_event_id: Mapped[str] = mapped_column(String(255), unique=True)
    payment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payments.id", ondelete="RESTRICT"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(50))
    # Raw payload kept for auditing/debugging disputes with the provider.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[WebhookEventStatus] = mapped_column(
        Enum(
            WebhookEventStatus,
            name="webhook_event_status",
            native_enum=False,
            create_constraint=True,
            length=20,
        ),
        default=WebhookEventStatus.RECEIVED,
    )
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
