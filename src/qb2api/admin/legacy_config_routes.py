"""One-cycle compatibility contract for legacy ``/api/config`` clients."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .dependencies import admin_state, require_admin

router = APIRouter()


@router.get("/api/config")
async def get_legacy_config(request: Request) -> dict[str, Any]:
    await require_admin(request)
    settings = admin_state(request).settings
    return {
        "status": "deprecated",
        "server": {
            "host": settings.control_host,
            "port": settings.control_port,
            "log_level": settings.log_level,
        },
        "auth": {
            "proxy_key_configured": bool(settings.proxy_api_key),
            "admin_key_configured": bool(settings.admin_key),
        },
        "providers": {
            "codebuddy": {"endpoint": settings.codebuddy_endpoint},
            "qoder": {"timeout_seconds": settings.qoder_timeout},
        },
        "migration_hint": "Use /api/admin/settings and /api/admin/accounts for runtime management.",
    }


@router.patch("/api/config")
async def update_legacy_config(request: Request) -> JSONResponse:
    await require_admin(request)
    return JSONResponse(
        status_code=410,
        content={
            "error": "legacy_config_removed",
            "migration_hint": "Use /api/admin/settings; environment files are not modified by the API.",
        },
    )
