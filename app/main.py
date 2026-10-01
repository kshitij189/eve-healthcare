import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from redis import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.routes import auth, bookings, centres, diagnostic_tests, payments
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, log_event, request_id_var
from app.core.redis_client import get_redis
from app.db import engine

logger = logging.getLogger("app.access")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    app = FastAPI(
        title="EVE Diagnostics API",
        version="0.2.0",
        description=(
            "Diagnostic test bookings with simulated payments.\n\n"
            "Authenticate via `POST /auth/login`, then send `Authorization: Bearer <token>`. "
            "Catalogue reads are public; catalogue changes need an ADMIN account."
        ),
    )
    register_exception_handlers(app)

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Reuse the caller's id if it sent one (e.g. from a gateway), so logs join up end to end.
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            log_event(
                logger,
                "http.request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
            return response
        finally:
            request_id_var.reset(token)

    app.include_router(auth.router)
    app.include_router(centres.router)
    app.include_router(diagnostic_tests.router)
    app.include_router(bookings.router)
    app.include_router(payments.router)

    @app.get("/health", tags=["health"])
    def health() -> JSONResponse:
        """503 if the database is unreachable. Redis being down only degrades (cache, limits)."""
        checks = {"database": "ok", "redis": "ok"}
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError:
            checks["database"] = "unavailable"
        try:
            get_redis().ping()
        except RedisError:
            checks["redis"] = "unavailable"
        healthy = checks["database"] == "ok"
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={"status": "ok" if healthy else "unavailable", **checks},
        )

    return app


app = create_app()
