import json
import logging

from app.core.logging import JsonFormatter, TextFormatter, request_id_var
from app.tasks import webhook_retry_delay


def _record(**fields) -> logging.LogRecord:
    record = logging.LogRecord("app.test", logging.INFO, __file__, 1, "booking.created", None, None)
    record.fields = fields
    record.request_id = request_id_var.get()
    return record


def test_json_formatter_emits_event_fields_and_request_id():
    token = request_id_var.set("req-123")
    try:
        line = JsonFormatter().format(_record(booking_id="b1", amount="400.00"))
    finally:
        request_id_var.reset(token)

    entry = json.loads(line)
    assert entry["event"] == "booking.created"
    assert entry["level"] == "INFO"
    assert entry["request_id"] == "req-123"
    assert entry["booking_id"] == "b1"
    assert entry["amount"] == "400.00"


def test_text_formatter_uses_key_value_pairs():
    line = TextFormatter().format(_record(booking_id="b1"))

    assert "event=booking.created" in line
    assert "booking_id=b1" in line


def test_webhook_retry_backoff_is_exponential_and_capped():
    assert [webhook_retry_delay(n) for n in range(7)] == [10, 20, 40, 80, 160, 300, 300]
