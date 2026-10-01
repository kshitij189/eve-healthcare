from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    # Cache + rate-limit counters. The Celery broker uses a separate Redis database.
    redis_url: str = "redis://localhost:6380/0"
    celery_broker_url: str = "redis://localhost:6380/1"
    # Tests run tasks inline instead of through a broker.
    celery_task_always_eager: bool = False

    # No defaults for secrets: the app should refuse to start rather than run with a known key.
    jwt_secret: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = Field(default=15, gt=0)
    refresh_token_expire_days: int = Field(default=7, gt=0)

    webhook_secret: str = Field(min_length=16)
    # Signed webhooks older than this are rejected, so captured requests can't be replayed later.
    webhook_tolerance_seconds: int = Field(default=300, gt=0)

    # Unpaid PENDING bookings expire after this long.
    booking_payment_window_minutes: int = Field(default=30, gt=0)

    catalogue_cache_ttl_seconds: int = Field(default=300, gt=0)
    rate_limit_enabled: bool = True
    rate_limit_auth_per_minute: int = Field(default=10, gt=0)
    rate_limit_writes_per_minute: int = Field(default=30, gt=0)

    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"

    # Only used by `python -m app.seed` for local development.
    seed_admin_email: str | None = None
    seed_admin_password: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
