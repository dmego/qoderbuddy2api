"""FastAPI Control Plane entrypoint.

The Control Plane owns administration, storage, and schedulers. It never
registers a Provider pool; the Proxy Worker owns model request execution.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from qb2api.admin.auth import classify_path, verify_admin_key, verify_proxy_key
from qb2api.admin.legacy_config_routes import router as legacy_config_router
from qb2api.admin.router import router as admin_router
from qb2api.config import Settings
from qb2api.runtime import RuntimeServices

from .internal_routes import router as internal_router
from .runtime_snapshot import RuntimeSnapshotService
from .service_models import ServiceSnapshot
from .service_router import router as service_router
from .supervisor import ServiceSupervisor

_WEB_ROOT = Path(__file__).resolve().parents[1] / "web"
_WEB_DIR = _WEB_ROOT / "dist" if (_WEB_ROOT / "dist").is_dir() else _WEB_ROOT


def create_control_app(
    settings_factory: Callable[[], Settings] | None = None,
    *,
    supervisor_factory: Callable[..., ServiceSupervisor] | None = None,
) -> FastAPI:
    factory = settings_factory or Settings.from_env
    application = FastAPI(title="2api Control Plane", version="1.0.0", lifespan=_lifespan)
    application.state.settings_factory = factory
    application.state.supervisor_factory = supervisor_factory or ServiceSupervisor
    application.state.settings = factory()
    application.state.role = "control"
    _install_static(application)
    _install_routes(application)
    _register_middleware(application)
    return application


@asynccontextmanager
async def _lifespan(application: FastAPI):
    settings = _ensure_internal_token(application.state.settings_factory())
    application.state.settings = settings
    runtime = await RuntimeServices.start(settings)
    runtime.attach(application)
    supervisor = application.state.supervisor_factory(
        settings,
        state_writer=_state_writer(runtime.account_repo),
        operation_writer=_operation_writer(runtime.account_repo),
    )
    application.state.supervisor = supervisor
    snapshot_service = RuntimeSnapshotService(runtime)
    application.state.runtime_snapshot_service = snapshot_service

    async def refresh_runtime() -> None:
        await runtime.refresh_accounts()
        snapshot_service.bump()
        if supervisor.snapshot.desired_state == "RUNNING":
            operation = await supervisor.reload(
                idempotency_key=f"runtime-snapshot-{snapshot_service.version}"
            )
            if operation.status != "succeeded":
                raise RuntimeError(operation.error or "worker runtime reload failed")

    application.state.refresh_provider_pools = refresh_runtime
    if runtime.account_repo is not None:
        saved = await runtime.account_repo.get_service_runtime("proxy-worker")
        await supervisor.restore(saved)
    autostart = None
    if settings.worker_autostart:
        autostart = asyncio.create_task(
            supervisor.start(idempotency_key="control-plane-autostart")
        )
        application.state.autostart_task = autostart
    try:
        yield
    finally:
        if autostart is not None and not autostart.done():
            await autostart
        await supervisor.stop(idempotency_key="control-plane-shutdown")
        await runtime.close()


def _ensure_internal_token(settings: Settings) -> Settings:
    if settings.worker_internal_token:
        return settings
    data_dir = Path(settings.data_dir)
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    token_path = data_dir / "worker.internal"
    if token_path.exists():
        token = token_path.read_text().strip()
    else:
        token = secrets.token_urlsafe(32)
        descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(token)
    if not token:
        raise RuntimeError("worker internal token file is empty")
    token_path.chmod(0o600)
    return replace(settings, worker_internal_token=token)


def _state_writer(repository: Any):
    async def write(snapshot: ServiceSnapshot) -> None:
        if repository is not None:
            await repository.save_service_runtime("proxy-worker", _snapshot_values(snapshot))

    return write


def _operation_writer(repository: Any):
    async def write(operation: Any) -> None:
        if repository is not None:
            await repository.save_service_operation(operation)

    return write


def _snapshot_values(snapshot: ServiceSnapshot) -> dict[str, Any]:
    identity = snapshot.identity
    return {
        "desired_state": snapshot.desired_state,
        "observed_state": snapshot.observed_state,
        "worker_pid": identity.pid if identity else None,
        "process_start_time": identity.process_start_time if identity else None,
        "process_group_id": identity.process_group_id if identity else None,
        "owner_instance_id": identity.owner_instance_id if identity else None,
        "internal_auth_version": identity.internal_auth_version if identity else None,
        "started_at": _iso(snapshot.started_at),
        "stopped_at": _iso(snapshot.stopped_at),
        "last_health_at": _iso(snapshot.last_health_at),
        "last_exit_code": snapshot.last_exit_code,
        "last_error": snapshot.last_error,
        "in_flight": snapshot.in_flight,
    }


def _iso(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, UTC).isoformat() if value else None


def _install_routes(application: FastAPI) -> None:
    application.include_router(admin_router)
    application.include_router(legacy_config_router)
    application.include_router(service_router)
    application.include_router(internal_router)

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "component": "control-plane", "worker": "unknown"}

    @application.get("/version")
    async def version() -> dict[str, str]:
        return {"version": "1.0.0", "component": "control-plane"}

    @application.get("/admin")
    @application.get("/admin/")
    async def admin_root(request: Request) -> FileResponse:
        return _shell_response(request)

    @application.get("/admin/{path:path}")
    async def admin_fallback(request: Request, path: str) -> FileResponse:
        if path.startswith("assets/"):
            raise HTTPException(status_code=404, detail="asset not found")
        return _shell_response(request)


def _shell_response(request: Request) -> FileResponse:
    if not request.app.state.settings.admin_ui_enabled:
        raise HTTPException(status_code=404, detail="admin UI disabled")
    index = _WEB_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="admin UI not packaged")
    return FileResponse(index, media_type="text/html; charset=utf-8")


def _install_static(application: FastAPI) -> None:
    if not _WEB_DIR.is_dir():
        return
    asset_dir = _WEB_DIR / "assets" if (_WEB_DIR / "assets").is_dir() else _WEB_DIR
    application.mount("/admin/assets", StaticFiles(directory=str(asset_dir)), name="control-assets")


async def _auth_middleware(request: Request, call_next):
    settings = request.app.state.settings
    path = request.url.path
    if path.startswith("/api/control/"):
        expected = settings.worker_internal_token
        presented = request.headers.get("X-QB2API-Worker-Token", "")
        if not expected or not hmac.compare_digest(expected, presented):
            return JSONResponse(status_code=401, content={"detail": "internal authentication required"})
        return await call_next(request)
    if path.startswith("/admin") and not settings.admin_ui_enabled:
        return JSONResponse(status_code=404, content={"detail": "admin UI disabled"})
    path_class = classify_path(request.method, path)
    if path_class in ("public_existing", "public_admin_bootstrap"):
        return await call_next(request)
    if path_class == "admin_protected":
        auth = request.headers.get("Authorization")
        if verify_admin_key(auth, settings) or request.cookies.get("qb2api_admin_session"):
            return await call_next(request)
        if settings.proxy_api_key and verify_proxy_key(auth, settings):
            return JSONResponse(status_code=403, content={"error": {"code": "forbidden"}})
        return JSONResponse(status_code=401, content={"error": {"code": "unauthorized"}})
    if path_class == "admin_legacy_private":
        auth = request.headers.get("Authorization")
        if verify_admin_key(auth, settings):
            return await call_next(request)
        if settings.proxy_api_key and verify_proxy_key(auth, settings):
            return JSONResponse(status_code=403, content={"error": {"code": "forbidden"}})
        return JSONResponse(status_code=401, content={"error": {"code": "unauthorized"}})
    return JSONResponse(status_code=404, content={"detail": "not found"})


def _register_middleware(application: FastAPI) -> None:
    application.middleware("http")(_auth_middleware)


create_control_app.__doc__ = "Create a persistent admin-only Control Plane application."

app = create_control_app()
