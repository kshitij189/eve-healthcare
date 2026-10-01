"""Read-through cache for public catalogue responses.

Invalidation uses a version number instead of deleting keys: every key embeds the current
catalogue version, and any admin write bumps it. Old entries simply stop being read and expire
via TTL. A reader reads the version *before* loading from the database, so data loaded just
before a write is stored under the old version and can never be served after it.
"""

import json
import logging
from collections.abc import Callable
from typing import Any

from redis import RedisError

from app.core.config import get_settings
from app.core.logging import log_event
from app.core.redis_client import get_redis

logger = logging.getLogger(__name__)

_VERSION_KEY = "catalogue:version"


def cached_catalogue(key: str, load: Callable[[], Any]) -> Any:
    """Return the JSON-serialisable value for `key`, loading it from the DB on a miss."""
    try:
        redis = get_redis()
        version = redis.get(_VERSION_KEY) or "0"
        full_key = f"catalogue:v{version}:{key}"
        hit = redis.get(full_key)
        if hit is not None:
            return json.loads(hit)
    except RedisError as exc:
        log_event(logger, "cache.unavailable", logging.WARNING, error=str(exc))
        return load()

    value = load()
    try:
        redis.set(full_key, json.dumps(value), ex=get_settings().catalogue_cache_ttl_seconds)
    except RedisError as exc:
        log_event(logger, "cache.unavailable", logging.WARNING, error=str(exc))
    return value


def invalidate_catalogue() -> None:
    try:
        get_redis().incr(_VERSION_KEY)
    except RedisError as exc:
        # Worst case, readers see the previous catalogue until the TTL expires.
        log_event(logger, "cache.invalidation_failed", logging.ERROR, error=str(exc))
