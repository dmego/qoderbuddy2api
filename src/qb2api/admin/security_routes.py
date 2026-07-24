"""Credential metadata and backup administration without secret disclosure."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .credential_routes import router as credential_router
from .dependencies import admin_state, require_admin
from .mutation_audit import audit_operation
from .validation import bounded_int, choice_filter, cursor_value, json_object

router = APIRouter()
router.include_router(credential_router)


@router.get("/backup")
async def list_backups(
    request: Request,
    *,
    limit: str | None = None,
    cursor: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    await require_admin(request)
    selected_limit = bounded_int(limit, default=50, maximum=100)
    selected_status = choice_filter(
        status, {"running", "succeeded", "failed", "cancelled"}, detail="invalid_status"
    )
    rows = await _repository(request).list_backup_runs(limit=500)
    if selected_status is not None:
        rows = [row for row in rows if row.get("status") == selected_status]
    offset = cursor_value(cursor, allow_zero=True) or 0
    page = rows[offset:offset + selected_limit]
    next_cursor = str(offset + selected_limit) if len(rows) > offset + selected_limit else None
    return {
        "backups": page,
        "limit": selected_limit,
        "next_cursor": next_cursor,
        "total": len(rows),
    }


@router.post("/backup")
async def create_backup(request: Request) -> dict[str, Any]:
    await require_admin(request)
    try:
        async with audit_operation(
            _repository(request), action="backup.create", resource_type="backup",
            resource_id="create", failure_code="backup_creation_failed",
        ):
            result = await _service(request).create()
    except Exception as error:
        raise HTTPException(status_code=422, detail="backup_creation_failed") from error
    return result


@router.get("/backup/{backup_id}")
async def get_backup(backup_id: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    service = _service(request)
    try:
        return await service.get(backup_id)
    except Exception as error:
        raise HTTPException(status_code=404, detail="backup_not_found") from error


@router.post("/backup/{backup_id}/restore")
async def validate_restore(backup_id: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    body = await json_object(request, allow_empty=True)
    if body.get("dry_run", True) is not True:
        raise HTTPException(status_code=409, detail="offline_restore_required")
    try:
        async with audit_operation(
            _repository(request), action="backup.restore.validate",
            resource_type="backup", resource_id=backup_id,
            failure_code="backup_validation_failed",
        ):
            result = await _service(request).validate_restore(backup_id)
    except Exception as error:
        raise HTTPException(status_code=422, detail="backup_validation_failed") from error
    return result


def _repository(request: Request):
    repository = getattr(admin_state(request), "account_repo", None)
    if repository is None:
        raise HTTPException(status_code=503, detail="repository_unavailable")
    return repository


def _service(request: Request):
    service = getattr(admin_state(request), "backup_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="backup_service_unavailable")
    return service
