"""Control Plane request authorization middleware."""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from qb2api.admin.auth import classify_path, verify_admin_key, verify_proxy_key
from qb2api.config import Settings


async def authenticate_request(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    error = _authorization_error(request, request.app.state.settings)
    if error is not None:
        return error
    return await call_next(request)


def _authorization_error(
    request: Request, settings: Settings
) -> JSONResponse | None:
    path = request.url.path
    if path.startswith("/api/control/"):
        return _internal_auth_error(request, settings)
    if path.startswith("/admin") and not settings.admin_ui_enabled:
        return _not_found("admin UI disabled")
    path_class = classify_path(request.method, path)
    if path_class in ("public_existing", "public_admin_bootstrap"):
        return None
    if path_class == "admin_protected":
        return _admin_auth_error(request, settings, allow_session=True)
    if path_class == "admin_legacy_private":
        return _admin_auth_error(request, settings, allow_session=False)
    return _not_found("not found")


def _internal_auth_error(
    request: Request, settings: Settings
) -> JSONResponse | None:
    expected = settings.worker_internal_token
    presented = request.headers.get("X-QB2API-Worker-Token", "")
    if expected and hmac.compare_digest(expected, presented):
        return None
    return JSONResponse(
        status_code=401,
        content={"detail": "internal authentication required"},
    )


def _admin_auth_error(
    request: Request,
    settings: Settings,
    *,
    allow_session: bool,
) -> JSONResponse | None:
    authorization = request.headers.get("Authorization")
    if verify_admin_key(authorization, settings):
        return None
    if allow_session and request.cookies.get("qb2api_admin_session"):
        return None
    if settings.proxy_api_key and verify_proxy_key(authorization, settings):
        return JSONResponse(status_code=403, content={"error": {"code": "forbidden"}})
    return JSONResponse(status_code=401, content={"error": {"code": "unauthorized"}})


def _not_found(detail: str) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": detail})
