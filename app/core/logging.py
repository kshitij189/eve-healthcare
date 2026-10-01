import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# Set per request by the middleware in main.py, so every log line of a request can be correlated.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Log a named event with structured fields, e.g. log_event(log, "booking.created", id=...)."""
    logger.log(level, event, extra={"fields": fields})


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        if getattr(record, "request_id", None):
            entry["request_id"] = record.request_id
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class TextFormatter(logging.Formatter):
    """Human-friendly variant for local development: same data, key=value."""

    def format(self, record: logging.LogRecord) -> str:
        parts = [
            self.formatTime(record),
            record.levelname,
            record.name,
            f"event={record.getMessage()}",
        ]
        if getattr(record, "request_id", None):
            parts.append(f"request_id={record.request_id}")
        parts += [f"{key}={value}" for key, value in getattr(record, "fields", {}).items()]
        line = " ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(level: str, log_format: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if log_format == "json" else TextFormatter())
    handler.addFilter(_RequestIdFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # We log our own access line with the request id; uvicorn's would be a duplicate.
    logging.getLogger("uvicorn.access").disabled = True
