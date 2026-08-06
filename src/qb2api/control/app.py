"""FastAPI Control Plane entrypoint.

The Control Plane owns administration, storage, and schedulers. It never
registers a Provider pool; the Proxy Worker owns model request execution.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from qb2api.admin.auth import classify_path
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
_HOP_HEADERS = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "proxy-connection",
    "te",
}


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
    application.middleware("http")(forward_proxy_requests)


async def forward_proxy_requests(request: Request, call_next: Callable):
    """Unified-port entry: forward every proxy-classified path to the Proxy Worker."""
    if classify_path(request.method, request.url.path) != "proxy_private":
        return await call_next(request)
    return await _relay_to_worker(request)


async def _relay_to_worker(request: Request) -> Response:
    settings = request.app.state.settings
    target = f"http://{settings.worker_host}:{settings.worker_port}"
    url = target + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP_HEADERS
    }
    body = await request.body()
    client = getattr(request.app.state, "proxy_forward_client", None)
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10))
    upstream = client.build_request(
        request.method,
        url,
        headers=headers,
        content=body or None,
    )
    try:
        response = await client.send(upstream, stream=True)
    except httpx.HTTPError:
        if owns_client:
            await client.aclose()
        return JSONResponse(
            status_code=502,
            content={"detail": "proxy worker unavailable"},
        )

    return StreamingResponse(
        _relay_iterator(response, client, owns_client),
        status_code=response.status_code,
        headers={"content-type": response.headers.get("content-type", "application/octet-stream")},
    )


async def _relay_iterator(response: httpx.Response, client: httpx.AsyncClient, owns_client: bool):
    try:
        async for chunk in response.aiter_bytes():
            yield chunk
    finally:
        await response.aclose()
        if owns_client:
            await client.aclose()


create_control_app.__doc__ = "Create a persistent admin-only Control Plane application."

app = create_control_app()
