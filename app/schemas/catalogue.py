import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from app.models import CentreTest
from app.schemas.common import RequestModel

# Quantised so "600" and "600.00" are stored and returned identically.
Price = Annotated[
    Decimal,
    Field(gt=0, max_digits=10, decimal_places=2),
    AfterValidator(lambda price: price.quantize(Decimal("0.01"))),
]


def _reject_null(value: object) -> object:
    # Fields are optional in PATCH bodies, but an explicit null for a required column is invalid.
    if value is None:
        raise ValueError("must not be null")
    return value


class CentreCreate(RequestModel):
    name: str = Field(min_length=1, max_length=200)
    location: str = Field(min_length=1, max_length=255)


class CentreUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    location: str | None = Field(default=None, min_length=1, max_length=255)

    _not_null = field_validator("name", "location")(_reject_null)


class DiagnosticTestCreate(RequestModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class DiagnosticTestUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)

    _not_null = field_validator("name")(_reject_null)


class OfferingCreate(RequestModel):
    test_id: uuid.UUID
    price: Price


class OfferingUpdate(RequestModel):
    price: Price


class DiagnosticTestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class CentreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    location: str
    created_at: datetime
    updated_at: datetime


class OfferingOut(BaseModel):
    """A test as offered by a centre: the test's details plus that centre's price."""

    test_id: uuid.UUID
    name: str
    description: str | None
    price: Decimal

    @classmethod
    def from_model(cls, offering: CentreTest) -> "OfferingOut":
        return cls(
            test_id=offering.test_id,
            name=offering.test.name,
            description=offering.test.description,
            price=offering.price,
        )


class CentreDetailOut(CentreOut):
    tests: list[OfferingOut]
