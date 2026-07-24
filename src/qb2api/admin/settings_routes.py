"""Versioned runtime settings API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .dependencies import admin_state, require_admin
from .mutation_audit import add_audit
from .validation import json_object

router = APIRouter(prefix="/settings")

SETTING_SCHEMA: dict[str, dict[str, Any]] = {
    "service.worker.autostart": {"default": False, "apply_mode": "immediate", "type": bool},
    "service.worker.start_timeout_seconds": {"default": 30, "apply_mode": "immediate", "type": int},
    "checkin.enabled": {"default": False, "apply_mode": "scheduler_reschedule", "type": bool},
    "checkin.at": {"default": "00:10", "apply_mode": "scheduler_reschedule", "type": str},
    "checkin.timezone": {"default": "Asia/Shanghai", "apply_mode": "scheduler_reschedule", "type": str},
    "monitoring.metrics_interval_seconds": {"default": 900, "apply_mode": "immediate", "type": int},
    "usage.rollup_interval_seconds": {"default": 60, "apply_mode": "immediate", "type": int},
    "usage.detail_retention_days": {"default": 90, "apply_mode": "immediate", "type": int},
}


@router.get("")
async def get_settings(request: Request) -> dict[str, Any]:
    await require_admin(request)
    repository = _repository(request)
    stored = {item["key"]: item for item in await repository.list_runtime_settings()}
    values = []
    for key, definition in SETTING_SCHEMA.items():
        item = stored.get(key)
        values.append(item or _default_item(key, definition))
    return {"settings": values, "schema": _public_schema()}


@router.get("/schema")
async def get_settings_schema(request: Request) -> dict[str, Any]:
    await require_admin(request)
    return {"schema": _public_schema()}


@router.patch("")
async def patch_setting(request: Request) -> dict[str, Any]:
    await require_admin(request)
    body = await json_object(request)
    key = body.get("key")
    if key not in SETTING_SCHEMA or "value" not in body:
        raise HTTPException(status_code=400, detail="unknown_setting")
    definition = SETTING_SCHEMA[key]
    value = body["value"]
    if type(value) is not definition["type"]:
        raise HTTPException(status_code=400, detail="invalid_setting_type")
    runtime = _runtime(request)
    try:
        runtime.validate_setting(key, value)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    repository = _repository(request)
    try:
        version = await _persist_setting(
            repository,
            key=key,
            value=value,
            expected_version=body.get("value_version"),
            apply_mode=definition["apply_mode"],
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    try:
        status = await _apply_runtime(request, key, value)
    except Exception as error:
        await _record_setting_terminal(
            repository,
            key=key,
            status="failed",
            error_code="setting_apply_failed",
        )
        raise HTTPException(status_code=422, detail="setting_apply_failed") from error
    await _record_setting_terminal(
        repository,
        key=key,
        status=status,
    )
    return {"key": key, "value": value, "value_version": version,
            "apply_mode": definition["apply_mode"], "apply_status": status}


async def _persist_setting(
    repository: Any,
    *,
    key: str,
    value: Any,
    expected_version: int | None,
    apply_mode: str,
) -> int:
    async with repository.transaction():
        version = await repository.upsert_runtime_setting(
            key=key,
            value=value,
            expected_version=expected_version,
            apply_mode=apply_mode,
            apply_status="pending",
            updated_by="admin",
        )
        await add_audit(
            repository,
            action="settings.update",
            resource_type="setting",
            resource_id=key,
            metadata={"apply_status": "pending"},
        )
    return version


async def _record_setting_terminal(
    repository: Any,
    *,
    key: str,
    status: str,
    error_code: str | None = None,
) -> None:
    result = "failed" if error_code else "succeeded"
    async with repository.transaction():
        await repository.update_runtime_setting_status(
            key,
            status=status,
            last_error=error_code,
        )
        await add_audit(
            repository,
            action="settings.update",
            resource_type="setting",
            resource_id=key,
            result=result,
            metadata={"apply_status": status, **({"error_code": error_code} if error_code else {})},
        )


async def _apply_runtime(request: Request, key: str, value: Any) -> str:
    return await _runtime(request).apply_setting(key, value)


def _runtime(request: Request):
    runtime = getattr(admin_state(request), "runtime", None)
    if runtime is None:
        raise RuntimeError("runtime_unavailable")
    return runtime


def _default_item(key: str, definition: dict[str, Any]) -> dict[str, Any]:
    return {"key": key, "value": definition["default"], "value_version": 0,
            "source": "default", "apply_mode": definition["apply_mode"], "apply_status": "effective"}


def _public_schema() -> dict[str, dict[str, Any]]:
    return {
        key: {name: value for name, value in definition.items() if name != "type"}
        | {"type": definition["type"].__name__}
        for key, definition in SETTING_SCHEMA.items()
    }


def _repository(request: Request):
    repository = getattr(admin_state(request), "account_repo", None)
    if repository is None:
        raise HTTPException(status_code=503, detail="repository_unavailable")
    return repository
