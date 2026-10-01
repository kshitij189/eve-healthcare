"""refunds, booking expiry, webhook inbox, refresh tokens

Revision ID: 5b7d2e9c41a3
Revises: 8e529ebfe42b
Create Date: 2026-10-01 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5b7d2e9c41a3"
down_revision: str | None = "8e529ebfe42b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, condition)


def upgrade() -> None:
    # New states. This is why statuses are VARCHAR + CHECK: no ALTER TYPE needed.
    _replace_check(
        "bookings",
        "ck_bookings_booking_status",
        "status IN ('PENDING', 'CONFIRMED', 'FAILED', 'CANCELLED', 'EXPIRED')",
    )
    _replace_check(
        "payments",
        "ck_payments_payment_status",
        "status IN ('PENDING', 'SUCCESS', 'FAILED', 'REFUNDED')",
    )

    # Webhook events become an inbox processed by a worker. Rows that already exist were
    # processed synchronously, so they are backfilled as PROCESSED with one attempt.
    op.add_column(
        "payment_webhook_events",
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PROCESSED"),
    )
    op.alter_column("payment_webhook_events", "status", server_default=None)
    op.create_check_constraint(
        op.f("ck_payment_webhook_events_webhook_event_status"),
        "payment_webhook_events",
        "status IN ('RECEIVED', 'PROCESSED', 'IGNORED', 'FAILED')",
    )
    op.add_column(
        "payment_webhook_events",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
    )
    op.alter_column("payment_webhook_events", "attempts", server_default="0")
    op.add_column("payment_webhook_events", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column(
        "payment_webhook_events",
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE payment_webhook_events SET received_at = processed_at")
    op.alter_column(
        "payment_webhook_events",
        "received_at",
        nullable=False,
        server_default=sa.text("now()"),
    )
    op.alter_column("payment_webhook_events", "processed_at", nullable=True, server_default=None)
    op.create_index(
        "ix_payment_webhook_events_status_received_at",
        "payment_webhook_events",
        ["status", "received_at"],
    )

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_refresh_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_refresh_tokens_token_hash")),
    )
    op.create_index(op.f("ix_refresh_tokens_user_id"), "refresh_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_table("refresh_tokens")

    op.drop_index("ix_payment_webhook_events_status_received_at", "payment_webhook_events")
    op.execute(
        "UPDATE payment_webhook_events SET processed_at = received_at WHERE processed_at IS NULL"
    )
    op.alter_column(
        "payment_webhook_events", "processed_at", nullable=False, server_default=sa.text("now()")
    )
    op.drop_column("payment_webhook_events", "received_at")
    op.drop_column("payment_webhook_events", "last_error")
    op.drop_column("payment_webhook_events", "attempts")
    op.drop_constraint(
        op.f("ck_payment_webhook_events_webhook_event_status"),
        "payment_webhook_events",
        type_="check",
    )
    op.drop_column("payment_webhook_events", "status")

    # Map new states onto the closest old ones so the old constraints can be restored.
    op.execute("UPDATE payments SET status = 'SUCCESS' WHERE status = 'REFUNDED'")
    op.execute("UPDATE bookings SET status = 'CANCELLED' WHERE status = 'EXPIRED'")
    _replace_check(
        "payments", "ck_payments_payment_status", "status IN ('PENDING', 'SUCCESS', 'FAILED')"
    )
    _replace_check(
        "bookings",
        "ck_bookings_booking_status",
        "status IN ('PENDING', 'CONFIRMED', 'FAILED', 'CANCELLED')",
    )
