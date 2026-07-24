"""FastAPI Control Plane entrypoint.

The Control Plane owns administration, storage, and schedulers. It never
registers a Provider pool; the Proxy Worker owns model request execution.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from qb2api.admin.legacy_config_routes import router as legacy_config_router
from qb2api.admin.router import router as admin_router
from qb2api.config import Settings

from .internal_routes import router as internal_router
from .lifecycle import control_lifespan
from .request_auth import authenticate_request
from .service_router import router as service_router
from .supervisor import ServiceSupervisor

_WEB_DIST_DIR = Path(__file__).resolve().parents[1] / "web" / "dist"
_WEB_ASSETS_DIR = _WEB_DIST_DIR / "assets"


def create_control_app(
    settings_factory: Callable[[], Settings] | None = None,
    *,
    supervisor_factory: Callable[..., ServiceSupervisor] | None = None,
) -> FastAPI:
    factory = settings_factory or Settings.from_env
    application = FastAPI(
        title="2api Control Plane",
        version="1.0.0",
        lifespan=control_lifespan,
    )
    application.state.settings_factory = factory
    application.state.supervisor_factory = supervisor_factory or ServiceSupervisor
    application.state.settings = factory()
    application.state.role = "control"
    _install_static(application)
    _install_routes(application)
    _register_middleware(application)
    return application


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
    index = _WEB_DIST_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="admin UI not packaged")
    return FileResponse(index, media_type="text/html; charset=utf-8")


def _install_static(application: FastAPI) -> None:
    if not _WEB_ASSETS_DIR.is_dir():
        return
    application.mount(
        "/admin/assets",
        StaticFiles(directory=str(_WEB_ASSETS_DIR)),
        name="control-assets",
    )


def _register_middleware(application: FastAPI) -> None:
    application.middleware("http")(authenticate_request)


create_control_app.__doc__ = "Create a persistent admin-only Control Plane application."

app = create_control_app()
