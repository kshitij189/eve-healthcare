import uuid
from datetime import UTC, datetime, timedelta

import jwt

from app.core.config import get_settings
from app.core.security import (
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    is_valid_webhook_signature,
    sign_webhook_payload,
    verify_password,
)


def test_password_hash_verifies_and_is_not_plain_text():
    hashed = hash_password("correct horse")

    assert hashed != "correct horse"
    assert verify_password(hashed, "correct horse")
    assert not verify_password(hashed, "wrong horse")


def test_verify_password_handles_malformed_hash():
    assert not verify_password("not-a-real-hash", "anything")


def test_access_token_round_trip():
    user_id = uuid.uuid4()

    assert decode_access_token(create_access_token(user_id)) == user_id


def test_expired_token_is_rejected():
    settings = get_settings()
    expired = jwt.encode(
        {"sub": str(uuid.uuid4()), "exp": datetime.now(UTC) - timedelta(minutes=1)},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    assert decode_access_token(expired) is None


def test_token_signed_with_another_key_is_rejected():
    forged = jwt.encode(
        {"sub": str(uuid.uuid4()), "exp": datetime.now(UTC) + timedelta(minutes=5)},
        "some-other-secret-that-is-long-enough-for-hs256",
        algorithm="HS256",
    )

    assert decode_access_token(forged) is None


def test_garbage_token_is_rejected():
    assert decode_access_token("not.a.jwt") is None


def test_refresh_tokens_are_random_and_stored_hashed():
    first, second = generate_refresh_token(), generate_refresh_token()

    assert first != second
    assert hash_refresh_token(first) == hash_refresh_token(first)
    assert first not in hash_refresh_token(first)


NOW = 1_800_000_000


def test_webhook_signature_is_tied_to_exact_body_and_timestamp():
    body = b'{"event_id": "evt_1"}'
    signature = sign_webhook_payload(body, NOW)

    assert is_valid_webhook_signature(body, str(NOW), signature, now=NOW)
    assert not is_valid_webhook_signature(b'{"event_id": "evt_2"}', str(NOW), signature, now=NOW)
    # Moving the timestamp forward to dodge the age check breaks the signature.
    assert not is_valid_webhook_signature(body, str(NOW + 1), signature, now=NOW)
    assert not is_valid_webhook_signature(body, str(NOW), "0" * 64, now=NOW)


def test_old_webhook_signatures_are_rejected():
    body = b"{}"
    tolerance = get_settings().webhook_tolerance_seconds
    sent_at = NOW - tolerance - 1

    signature = sign_webhook_payload(body, sent_at)

    assert not is_valid_webhook_signature(body, str(sent_at), signature, now=NOW)
    assert is_valid_webhook_signature(body, str(sent_at), signature, now=NOW - 2)


def test_non_numeric_webhook_timestamp_is_rejected():
    assert not is_valid_webhook_signature(b"{}", "yesterday", "x" * 64)
