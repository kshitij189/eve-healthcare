"""Stand-in for the external payment gateway.

A real integration would make HTTP calls here (ideally outside the DB transaction, or from a
background job with retries). The simulation answers immediately and deterministically.
"""

import logging
import uuid

from app.core.logging import log_event
from app.models import Payment, PaymentStatus

logger = logging.getLogger(__name__)


def new_provider_payment_id() -> str:
    return f"pay_{uuid.uuid4().hex}"


def refund(payment: Payment, reason: str) -> None:
    payment.transition_to(PaymentStatus.REFUNDED)
    log_event(
        logger,
        "payment.refunded",
        payment_id=payment.id,
        booking_id=payment.booking_id,
        amount=payment.amount,
        reason=reason,
    )
