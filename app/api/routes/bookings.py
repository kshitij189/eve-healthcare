import uuid

from fastapi import APIRouter, Depends, status

from app.api.dependencies import CurrentUser, DbSession, PaginationParams, rate_limit_by_user
from app.schemas.booking import BookingCreate, BookingOut
from app.schemas.common import Page
from app.services import booking_service

router = APIRouter(prefix="/bookings", tags=["bookings"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=BookingOut,
    dependencies=[Depends(rate_limit_by_user("bookings"))],
)
def create_booking(data: BookingCreate, db: DbSession, user: CurrentUser) -> BookingOut:
    """Book a test at a centre. The amount is taken from the centre's current price.

    The booking must be paid within the payment window (30 minutes by default) or it expires.
    """
    return BookingOut.from_model(booking_service.create_booking(db, user, data))


@router.get("", response_model=Page[BookingOut])
def list_bookings(db: DbSession, user: CurrentUser, page: PaginationParams) -> Page[BookingOut]:
    """The authenticated user's own bookings, newest first."""
    bookings, total = booking_service.list_user_bookings(
        db, user, limit=page.limit, offset=page.offset
    )
    return Page(
        items=[BookingOut.from_model(b) for b in bookings],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{booking_id}", response_model=BookingOut)
def get_booking(booking_id: uuid.UUID, db: DbSession, user: CurrentUser) -> BookingOut:
    return BookingOut.from_model(booking_service.get_user_booking(db, user, booking_id))


@router.post("/{booking_id}/cancel", response_model=BookingOut)
def cancel_booking(booking_id: uuid.UUID, db: DbSession, user: CurrentUser) -> BookingOut:
    """Cancel a PENDING booking, or a CONFIRMED one before its appointment (payment refunded)."""
    return BookingOut.from_model(booking_service.cancel_booking(db, user, booking_id))
