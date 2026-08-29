"""Independent Proxy Worker ASGI entrypoint.

The Worker owns the proxy-only boundary, protocol routes, runtime and
telemetry. Control Plane routes are rejected before reaching the handlers.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from qb2api.admin.auth import classify_path
from qb2api.config import Settings
from qb2api.worker.telemetry import WorkerTelemetry

from .anthropic_routes import router as anthropic_router
from .metadata_routes import router as metadata_router
from .openai_routes import router as openai_router
from .proxy_state import ProxyState, SnapshotLoader

_BLOCKED_PREFIXES = ("/admin", "/static/admin", "/api/admin", "/api/config")


def create_worker_app(
    settings_factory: Callable[[], Settings] | None = None,
    *,
    snapshot_loader: SnapshotLoader | None = None,
) -> FastAPI:
    factory = settings_factory or Settings.from_env
    application = FastAPI(
        title="2api Proxy Worker",
        version="1.0.0",
        lifespan=_worker_lifespan,
    )
    application.state.settings_factory = factory
    application.state.snapshot_loader = snapshot_loader
    application.state.role = "worker"
    _install_internal_routes(application)
    _install_boundary(application)
    _install_telemetry(application)
    _install_proxy_routes(application)
    return application


@asynccontextmanager
async def _worker_lifespan(application: FastAPI):
    settings = application.state.settings_factory()
    application.state.settings = settings
    control_host = settings.control_host
    if control_host in {"0.0.0.0", "::"}:
        control_host = "127.0.0.1"
    telemetry = WorkerTelemetry(
        endpoint=f"http://{control_host}:{settings.control_port}",
        token=os.getenv("QB2API_WORKER_INTERNAL_TOKEN") or settings.worker_internal_token,
    )
    application.state.telemetry = telemetry
    telemetry.start()
    state = ProxyState(settings, application.state.snapshot_loader)
    try:
        await state.start(application)
        yield
    finally:
        await state.close()
        await telemetry.stop()


def _install_boundary(application: FastAPI) -> None:
    @application.middleware("http")
    async def worker_boundary(request: Request, call_next):
        if request.url.path.startswith(_BLOCKED_PREFIXES):
            return JSONResponse(status_code=404, content={"detail": "worker-only endpoint"})
        if request.url.path.startswith("/internal/"):
            expected = request.app.state.settings.worker_internal_token
            presented = request.headers.get("X-QB2API-Worker-Token")
            if expected and presented != expected:
                return JSONResponse(status_code=401, content={"detail": "internal auth required"})
            return await call_next(request)
        path_class = classify_path(request.method, request.url.path)
        if path_class == "public_existing":
            return await call_next(request)
        state = getattr(request.app.state, "proxy_state", None)
        if state is None or not state.verify_proxy_auth(request.headers.get("Authorization")):
            return JSONResponse(status_code=401, content=_proxy_auth_error())
        return await call_next(request)


def _install_telemetry(application: FastAPI) -> None:
    @application.middleware("http")
    async def worker_telemetry(request: Request, call_next):
        if not request.url.path.startswith(("/v1/chat/completions", "/v1/messages")):
            return await call_next(request)
        request.state.telemetry_request_id = str(uuid.uuid4())
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception as error:
            _emit(request, started=started, status_code=502, error=error)
            raise
        original = response.body_iterator

        async def iterator():
            try:
                async for chunk in original:
                    yield chunk
            except Exception as error:
                _emit(
                    request,
                    started=started,
                    status_code=response.status_code,
                    error=error,
                )
                raise
            else:
                _emit(
                    request,
                    started=started,
                    status_code=response.status_code,
                    error=None,
                )

        response.body_iterator = iterator()
        return response


def _emit(
    request: Request,
    *,
    started: float,
    status_code: int,
    error: Exception | None,
) -> None:
    telemetry = getattr(request.app.state, "telemetry", None)
    context = getattr(request.state, "telemetry_context", None)
    if telemetry is None or not isinstance(context, dict):
        return
    chat_request = context.get("chat_request")
    usage = chat_request.telemetry if chat_request is not None else {}
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    telemetry.emit(
        {
            "event_id": str(uuid.uuid4()),
            "request_id": request.state.telemetry_request_id,
            "provider": context.get("provider") or usage.get("provider") or "unknown",
            "account_id": usage.get("account_id"),
            "model_id": context.get("model_id"),
            "protocol": context.get("protocol"),
            "status": "succeeded" if error is None and status_code < 400 else "failed",
            "http_status": status_code,
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "latency_ms": int((time.monotonic() - started) * 1000),
            "stream_committed": usage.get("stream_committed", False),
            "reasoning_effort": getattr(chat_request, "reasoning_effort", None),
            "started_at": now,
            "finished_at": now,
            "error_code": type(error).__name__ if error else None,
        }
    )


def _install_internal_routes(application: FastAPI) -> None:
    @application.get("/internal/health/live", include_in_schema=False)
    async def worker_live() -> dict[str, str]:
        return {
            "status": "ok",
            "component": "proxy-worker",
            "owner_instance_id": os.getenv("QB2API_WORKER_OWNER_INSTANCE_ID", "unknown"),
            "internal_auth_version": os.getenv("QB2API_WORKER_INTERNAL_AUTH_VERSION", "0"),
        }

    @application.get("/internal/health/ready", include_in_schema=False)
    async def worker_ready(request: Request) -> dict[str, str | int]:
        runtime = getattr(request.app.state, "runtime", None)
        if runtime is None:
            return {"status": "starting", "component": "proxy-worker"}
        return {
            "status": "ready",
            "component": "proxy-worker",
            "provider_count": len(runtime.providers.providers),
            "snapshot_version": runtime.snapshot_version,
            "owner_instance_id": os.getenv("QB2API_WORKER_OWNER_INSTANCE_ID", "unknown"),
            "internal_auth_version": os.getenv("QB2API_WORKER_INTERNAL_AUTH_VERSION", "0"),
        }

    @application.post("/internal/runtime/reload", include_in_schema=False)
    async def worker_reload(request: Request) -> dict[str, str | int]:
        state = getattr(request.app.state, "proxy_state", None)
        if state is None:
            return {"status": "starting", "snapshot_version": 0}
        await state.refresh()
        return {
            "status": "reloaded",
            "snapshot_version": state.runtime.snapshot_version,
            "owner_instance_id": os.getenv("QB2API_WORKER_OWNER_INSTANCE_ID", "unknown"),
            "internal_auth_version": os.getenv("QB2API_WORKER_INTERNAL_AUTH_VERSION", "0"),
        }


def _install_proxy_routes(application: FastAPI) -> None:
    application.include_router(metadata_router)
    application.include_router(openai_router)
    application.include_router(anthropic_router)


def _proxy_auth_error() -> dict[str, dict[str, str]]:
    return {
        "error": {
            "message": "Invalid or missing API key",
            "type": "auth_error",
            "code": "unauthorized",
        }
    }


app = create_worker_app()
