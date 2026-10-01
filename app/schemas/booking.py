import uuid
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import AwareDatetime, BaseModel, field_validator

from app.models import Booking, BookingStatus
from app.schemas.common import RequestModel


class BookingCreate(RequestModel):
    # Deliberately no amount/price field: the server takes the price from the centre's offering.
    centre_id: uuid.UUID
    test_id: uuid.UUID
    appointment_at: AwareDatetime

    @field_validator("appointment_at")
    @classmethod
    def must_be_in_future(cls, value: datetime) -> datetime:
        if value <= datetime.now(UTC):
            raise ValueError("appointment_at must be in the future")
        return value


class PatientOut(BaseModel):
    id: uuid.UUID
    name: str


class BookingCentreOut(BaseModel):
    id: uuid.UUID
    name: str
    location: str


class BookingTestOut(BaseModel):
    id: uuid.UUID
    name: str


class BookingOut(BaseModel):
    id: uuid.UUID
    patient: PatientOut
    centre: BookingCentreOut
    test: BookingTestOut
    appointment_at: datetime
    amount: Decimal
    status: BookingStatus
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, booking: Booking) -> "BookingOut":
        centre = booking.centre_test.centre
        test = booking.centre_test.test
        return cls(
            id=booking.id,
            patient=PatientOut(id=booking.user.id, name=booking.user.name),
            centre=BookingCentreOut(id=centre.id, name=centre.name, location=centre.location),
            test=BookingTestOut(id=test.id, name=test.name),
            appointment_at=booking.appointment_at,
            amount=booking.amount,
            status=booking.status,
            created_at=booking.created_at,
            updated_at=booking.updated_at,
        )
