import pytest

from app.core.errors import ConflictError
from app.models import Booking, BookingStatus, Payment, PaymentStatus
from app.models.booking import ALLOWED_TRANSITIONS
from app.models.payment import PAYMENT_TRANSITIONS

ALL_BOOKING_PAIRS = [(a, b) for a in BookingStatus for b in BookingStatus]
EXPECTED_BOOKING_MOVES = {
    (BookingStatus.PENDING, BookingStatus.CONFIRMED),
    (BookingStatus.PENDING, BookingStatus.FAILED),
    (BookingStatus.PENDING, BookingStatus.CANCELLED),
    (BookingStatus.PENDING, BookingStatus.EXPIRED),
    (BookingStatus.CONFIRMED, BookingStatus.CANCELLED),
}


@pytest.mark.parametrize(("current", "target"), ALL_BOOKING_PAIRS)
def test_booking_transitions_match_the_documented_state_machine(current, target):
    booking = Booking(status=current)

    if (current, target) in EXPECTED_BOOKING_MOVES:
        booking.transition_to(target)
        assert booking.status == target
    else:
        with pytest.raises(ConflictError) as exc_info:
            booking.transition_to(target)
        assert exc_info.value.code == "INVALID_STATE_TRANSITION"
        assert booking.status == current


def test_every_booking_status_has_a_transition_entry():
    assert set(ALLOWED_TRANSITIONS) == set(BookingStatus)


@pytest.mark.parametrize(
    ("current", "target", "allowed"),
    [
        (PaymentStatus.PENDING, PaymentStatus.SUCCESS, True),
        (PaymentStatus.PENDING, PaymentStatus.FAILED, True),
        (PaymentStatus.SUCCESS, PaymentStatus.REFUNDED, True),
        (PaymentStatus.FAILED, PaymentStatus.SUCCESS, False),
        (PaymentStatus.SUCCESS, PaymentStatus.FAILED, False),
        (PaymentStatus.PENDING, PaymentStatus.REFUNDED, False),
        (PaymentStatus.REFUNDED, PaymentStatus.SUCCESS, False),
    ],
)
def test_payment_transitions(current, target, allowed):
    payment = Payment(status=current)

    if allowed:
        payment.transition_to(target)
        assert payment.status == target
    else:
        with pytest.raises(ConflictError):
            payment.transition_to(target)
        assert payment.status == current


def test_every_payment_status_has_a_transition_entry():
    assert set(PAYMENT_TRANSITIONS) == set(PaymentStatus)
