import os

# Point the app at dedicated test stores, with fixed secrets, before anything imports it.
# Real env vars win over .env in pydantic-settings, so these override a developer's .env.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://eve:eve@localhost:5433/eve_test"
)
os.environ["REDIS_URL"] = os.environ.get("TEST_REDIS_URL", "redis://localhost:6380/15")
os.environ["JWT_SECRET"] = "test-jwt-secret-that-is-at-least-32-characters"
os.environ["WEBHOOK_SECRET"] = "test-webhook-secret"
# Tasks run inline at .delay(), so no broker or worker is needed and results are deterministic.
os.environ["CELERY_TASK_ALWAYS_EAGER"] = "true"
os.environ["RATE_LIMIT_ENABLED"] = "true"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["LOG_FORMAT"] = "text"
