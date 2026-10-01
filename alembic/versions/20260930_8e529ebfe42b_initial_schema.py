"""initial schema

Revision ID: 8e529ebfe42b
Revises:
Create Date: 2026-09-30 22:13:13.102617
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8e529ebfe42b"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    # Status/role columns are VARCHAR + CHECK rather than native Postgres ENUM types:
    # adding a value later is a simple constraint change instead of an ALTER TYPE.
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("role IN ('USER', 'ADMIN')", name=op.f("ck_users_user_role")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )

    op.create_table(
        "diagnostic_centres",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("location", sa.String(length=255), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_diagnostic_centres")),
    )

    op.create_table(
        "diagnostic_tests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_diagnostic_tests")),
        sa.UniqueConstraint("name", name=op.f("uq_diagnostic_tests_name")),
    )

    op.create_table(
        "centre_tests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("centre_id", sa.Uuid(), nullable=False),
        sa.Column("test_id", sa.Uuid(), nullable=False),
        sa.Column("price", sa.Numeric(precision=10, scale=2), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("price > 0", name=op.f("ck_centre_tests_price_positive")),
        sa.ForeignKeyConstraint(
            ["centre_id"],
            ["diagnostic_centres.id"],
            name=op.f("fk_centre_tests_centre_id_diagnostic_centres"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["test_id"],
            ["diagnostic_tests.id"],
            name=op.f("fk_centre_tests_test_id_diagnostic_tests"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_centre_tests")),
        sa.UniqueConstraint("centre_id", "test_id", name=op.f("uq_centre_tests_centre_id_test_id")),
    )
    op.create_index(op.f("ix_centre_tests_test_id"), "centre_tests", ["test_id"])

    op.create_table(
        "bookings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("centre_test_id", sa.Uuid(), nullable=False),
        sa.Column("appointment_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("amount", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('PENDING', 'CONFIRMED', 'FAILED', 'CANCELLED')",
            name=op.f("ck_bookings_booking_status"),
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_bookings_amount_positive")),
        sa.ForeignKeyConstraint(
            ["centre_test_id"],
            ["centre_tests.id"],
            name=op.f("fk_bookings_centre_test_id_centre_tests"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_bookings_user_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bookings")),
    )
    op.create_index(op.f("ix_bookings_centre_test_id"), "bookings", ["centre_test_id"])
    op.create_index("ix_bookings_user_id_created_at", "bookings", ["user_id", "created_at"])

    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("booking_id", sa.Uuid(), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("amount", sa.Numeric(precision=10, scale=2), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('PENDING', 'SUCCESS', 'FAILED')", name=op.f("ck_payments_payment_status")
        ),
        sa.ForeignKeyConstraint(
            ["booking_id"],
            ["bookings.id"],
            name=op.f("fk_payments_booking_id_bookings"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
        sa.UniqueConstraint("booking_id", name=op.f("uq_payments_booking_id")),
        sa.UniqueConstraint("provider_payment_id", name=op.f("uq_payments_provider_payment_id")),
    )

    op.create_table(
        "payment_webhook_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider_event_id", sa.String(length=255), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payments.id"],
            name=op.f("fk_payment_webhook_events_payment_id_payments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_webhook_events")),
        # The idempotency guarantee for webhooks rests on this constraint.
        sa.UniqueConstraint(
            "provider_event_id", name=op.f("uq_payment_webhook_events_provider_event_id")
        ),
    )
    op.create_index(
        op.f("ix_payment_webhook_events_payment_id"), "payment_webhook_events", ["payment_id"]
    )


def downgrade() -> None:
    op.drop_table("payment_webhook_events")
    op.drop_table("payments")
    op.drop_table("bookings")
    op.drop_table("centre_tests")
    op.drop_table("diagnostic_tests")
    op.drop_table("diagnostic_centres")
    op.drop_table("users")
