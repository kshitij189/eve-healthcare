import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, Numeric
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.errors import ConflictError
from app.db import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.catalogue import CentreTest
from app.models.user import User

if TYPE_CHECKING:
    from app.models.payment import Payment


class BookingStatus(StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    # Not paid within the payment window, or the appointment time passed while unpaid.
    EXPIRED = "EXPIRED"


# Every status change goes through Booking.transition_to, so this table is the whole state machine.
ALLOWED_TRANSITIONS: dict[BookingStatus, frozenset[BookingStatus]] = {
    BookingStatus.PENDING: frozenset(
        {
            BookingStatus.CONFIRMED,
            BookingStatus.FAILED,
            BookingStatus.CANCELLED,
            BookingStatus.EXPIRED,
        }
    ),
    # Cancelling a confirmed booking refunds its payment.
    BookingStatus.CONFIRMED: frozenset({BookingStatus.CANCELLED}),
    BookingStatus.FAILED: frozenset(),
    BookingStatus.CANCELLED: frozenset(),
    BookingStatus.EXPIRED: frozenset(),
}


class Booking(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_bookings_user_id_created_at", "user_id", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    # RESTRICT: an offering (or its centre/test) can't be deleted once someone has booked it.
    centre_test_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("centre_tests.id", ondelete="RESTRICT"), index=True
    )
    appointment_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Snapshot of the offering's price at booking time; later price changes must not affect it.
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[BookingStatus] = mapped_column(
        Enum(
            BookingStatus,
            name="booking_status",
            native_enum=False,
            create_constraint=True,
            length=20,
        ),
        default=BookingStatus.PENDING,
    )

    user: Mapped[User] = relationship(lazy="joined")
    centre_test: Mapped[CentreTest] = relationship(lazy="joined")
    payment: Mapped["Payment | None"] = relationship(back_populates="booking")

    def can_transition_to(self, new_status: BookingStatus) -> bool:
        return new_status in ALLOWED_TRANSITIONS[self.status]

    def transition_to(self, new_status: BookingStatus) -> None:
        if not self.can_transition_to(new_status):
            raise ConflictError(
                f"Booking cannot move from {self.status} to {new_status}",
                code="INVALID_STATE_TRANSITION",
            )
        self.status = new_status
