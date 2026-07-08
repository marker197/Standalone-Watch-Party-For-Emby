"""Emby-Trakt Watch Party — application bootstrap.

Starts:
  - FastAPI REST server on port 8000
  - Socket.IO WebSocket server on same ASGI app
  - Database init
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager

import socketio
import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.utils.logging import setup_logging, security_log
from app.utils.database import init_db
from app.utils.redis_cache import close_redis
from app.api.routes import router
from app.services.watch_party.service import sio as watch_party_sio, close_service

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Security middleware for audit logging (pure ASGI — does NOT block Socket.IO)
# ---------------------------------------------------------------------------

class SecurityAuditMiddleware:
    """Pure ASGI middleware that logs requests without wrapping them in
    BaseHTTPMiddleware, which would intercept the Socket.IO mount and
    cause 404s on /ws/socket.io."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        start_time = time.time()
        path = scope.get("path", "")
        method = scope.get("method", "WS")
        client = scope.get("client")
        client_ip = client[0] if client else "unknown"

        security_log.info("http_request_received",
            method=method, path=path, client_ip=client_ip,
        )

        status_code = None

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 0)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
            duration = time.time() - start_time
            if status_code and status_code >= 400:
                security_log.warning("http_response_error",
                    method=method, path=path,
                    status_code=status_code,
                    duration_ms=duration * 1000,
                )
        except Exception as e:
            security_log.error("http_request_exception",
                method=method, path=path, error=str(e),
            )
            raise


# ---------------------------------------------------------------------------
# Lifespan: startup / shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    log.info("watch_party.starting")

    # Database
    await init_db()
    log.info("watch_party.db_ready")

    yield  # app is running

    # Shutdown
    await close_service()
    await close_redis()
    log.info("watch_party.shutdown")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Emby-Trakt Watch Party",
    version="1.0.0",
    description="Synchronized Watch Party with Trakt Scrobbling",
    lifespan=lifespan,
)

# Security middleware:

def _get_allowed_hosts() -> list[str]:
    hosts = os.environ.get("ALLOWED_HOSTS", "*").split(",")
    return [h.strip() for h in hosts if h.strip()]

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=_get_allowed_hosts(),
)

def _get_allowed_origins() -> list[str]:
    allowed = os.environ.get("ALLOWED_ORIGINS", "http://localhost:8000").split(",")
    return [o.strip() for o in allowed if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_get_allowed_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    expose_headers=["Content-Length", "Content-Type"],
    max_age=600,
)

app.add_middleware(SecurityAuditMiddleware)

# REST routes
app.include_router(router)


# ---------------------------------------------------------------------------
# Dashboard (served at /)
# ---------------------------------------------------------------------------

templates = Jinja2Templates(directory="frontend/templates")


@app.get("/")
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
    })


# ---------------------------------------------------------------------------
# Mount Socket.IO for Watch Party
# ---------------------------------------------------------------------------
# Socket.IO as the outer ASGI app, FastAPI as fallback.
# app.mount("/ws", ...) is incompatible with any ASGI middleware wrapping —
# Starlette can't route to mounted sub-apps through middleware layers.
# The python-socketio recommended pattern wraps FastAPI instead.

_fastapi_app = app
app = socketio.ASGIApp(
    watch_party_sio,
    other_asgi_app=_fastapi_app,
    socketio_path="/ws/socket.io",
)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        log_level=settings.log_level.lower(),
    )
