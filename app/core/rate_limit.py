"""Fixed-window rate limiting backed by Redis (shared across API instances)."""

import logging
import time

from redis import RedisError

from app.core.config import get_settings
from app.core.errors import RateLimitedError
from app.core.logging import log_event
from app.core.redis_client import get_redis

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60


def enforce_rate_limit(scope: str, identity: str, limit: int) -> None:
    """Allow `limit` requests per minute for (scope, identity); raise RateLimitedError beyond it.

    Fixed windows can let through up to 2x the limit across a window boundary; that's an
    acceptable trade for a single INCR per request.
    """
    if not get_settings().rate_limit_enabled:
        return

    now = time.time()
    window = int(now // WINDOW_SECONDS)
    key = f"ratelimit:{scope}:{identity}:{window}"
    try:
        pipe = get_redis().pipeline()
        pipe.incr(key)
        pipe.expire(key, WINDOW_SECONDS)
        count, _ = pipe.execute()
    except RedisError as exc:
        # Fail open: losing rate limiting briefly is better than rejecting every request.
        log_event(logger, "rate_limit.unavailable", logging.WARNING, error=str(exc))
        return

    if count > limit:
        retry_after = WINDOW_SECONDS - int(now % WINDOW_SECONDS)
        log_event(logger, "rate_limit.exceeded", logging.WARNING, scope=scope, identity=identity)
        raise RateLimitedError(retry_after_seconds=max(retry_after, 1))
