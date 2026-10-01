# Importing every model here registers them on Base.metadata (needed by Alembic).
from app.models.booking import Booking, BookingStatus
from app.models.catalogue import CentreTest, DiagnosticCentre, DiagnosticTest
from app.models.payment import Payment, PaymentStatus, PaymentWebhookEvent, WebhookEventStatus
from app.models.refresh_token import RefreshToken
from app.models.user import User, UserRole

__all__ = [
    "Booking",
    "BookingStatus",
    "CentreTest",
    "DiagnosticCentre",
    "DiagnosticTest",
    "Payment",
    "PaymentStatus",
    "PaymentWebhookEvent",
    "RefreshToken",
    "User",
    "UserRole",
    "WebhookEventStatus",
]
