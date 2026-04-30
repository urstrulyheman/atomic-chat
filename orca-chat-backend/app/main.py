from contextlib import asynccontextmanager
import logging
import re
import time
from uuid import uuid4

import redis
from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import settings
from app.database import SessionLocal, init_db
from app.modules.admin.routes import router as admin_router
from app.modules.auth.routes import router as auth_router
from app.modules.chat.routes import router as chat_router
from app.modules.payments.routes import router as payments_router
from app.modules.users.routes import router as users_router
from app.modules.wallet.routes import router as wallet_router

REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,80}$")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
logger = logging.getLogger("orca_chat.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.auto_create_tables:
        init_db()
    yield


app = FastAPI(title="Orca Chat Coin MVP", lifespan=lifespan)

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=settings.allowed_host_list,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[REQUEST_ID_HEADER],
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    incoming_request_id = request.headers.get(REQUEST_ID_HEADER, "")
    request_id = incoming_request_id if REQUEST_ID_PATTERN.fullmatch(incoming_request_id) else str(uuid4())
    request.state.request_id = request_id
    started_at = time.perf_counter()

    try:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                body_size = int(content_length)
            except ValueError:
                response = JSONResponse({"detail": "Invalid Content-Length header"}, status_code=status.HTTP_400_BAD_REQUEST)
                return finalize_response(request, response, request_id, started_at)
            if body_size > settings.max_request_body_bytes:
                response = JSONResponse({"detail": "Request body too large"}, status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
                return finalize_response(request, response, request_id, started_at)
        response = await call_next(request)
    except Exception:
        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        logger.exception(
            "request_failed request_id=%s method=%s path=%s duration_ms=%s",
            request_id,
            request.method,
            request.url.path,
            duration_ms,
        )
        raise

    return finalize_response(request, response, request_id, started_at)


def finalize_response(request: Request, response: Response, request_id: str, started_at: float) -> Response:
    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
    response.headers[REQUEST_ID_HEADER] = request_id
    response.headers["X-Response-Time-ms"] = str(duration_ms)
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    if settings.hsts_enabled:
        response.headers.setdefault(
            "Strict-Transport-Security",
            f"max-age={settings.hsts_max_age_seconds}; includeSubDomains",
        )
    if settings.access_log_enabled:
        logger.info(
            "request_completed request_id=%s method=%s path=%s status_code=%s duration_ms=%s",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
    return response


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready(response: Response):
    checks = {"database": "ok", "redis": "skipped"}

    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except Exception:
        checks["database"] = "error"

    if settings.readiness_check_redis:
        try:
            client = redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
            client.ping()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "error"

    if any(value == "error" for value in checks.values()):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "checks": checks}

    return {"status": "ready", "checks": checks}


app.include_router(auth_router)
app.include_router(users_router)
app.include_router(wallet_router)
app.include_router(payments_router)
app.include_router(chat_router)
app.include_router(admin_router)
