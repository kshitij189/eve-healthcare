from functools import lru_cache

from redis import Redis

from app.core.config import get_settings


@lru_cache
def get_redis() -> Redis:
    # Short timeouts: Redis only backs optional features (cache, rate limits), so a slow or
    # unreachable Redis should cost milliseconds and fall back, not stall requests.
    return Redis.from_url(
        get_settings().redis_url,
        socket_timeout=0.5,
        socket_connect_timeout=0.5,
        decode_responses=True,
    )
