import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DiagnosticCentre(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "diagnostic_centres"

    name: Mapped[str] = mapped_column(String(200))
    location: Mapped[str] = mapped_column(String(255))

    # passive_deletes: let the DB's ON DELETE CASCADE / RESTRICT decide, instead of the ORM.
    offerings: Mapped[list["CentreTest"]] = relationship(
        back_populates="centre", passive_deletes=True, order_by="CentreTest.created_at"
    )


class DiagnosticTest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "diagnostic_tests"

    # A test type (e.g. "Complete Blood Count") exists once; per-centre pricing lives on CentreTest.
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str | None] = mapped_column(Text)

    offerings: Mapped[list["CentreTest"]] = relationship(
        back_populates="test", passive_deletes=True
    )


class CentreTest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A test offered by a specific centre, at that centre's price."""

    __tablename__ = "centre_tests"
    __table_args__ = (
        UniqueConstraint("centre_id", "test_id"),
        CheckConstraint("price > 0", name="price_positive"),
    )

    centre_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("diagnostic_centres.id", ondelete="CASCADE")
    )
    # The (centre_id, test_id) unique index already covers lookups by centre_id.
    test_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("diagnostic_tests.id", ondelete="CASCADE"), index=True
    )
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))

    # Always needed when rendering an offering or a booking, and many-to-one joins are cheap.
    centre: Mapped[DiagnosticCentre] = relationship(back_populates="offerings", lazy="joined")
    test: Mapped[DiagnosticTest] = relationship(back_populates="offerings", lazy="joined")
