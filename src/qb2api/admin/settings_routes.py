"""Versioned runtime settings API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from qb2api.checkin.scheduler import ScheduleConfiguration
from qb2api.control.settings import SettingsApplier

from .dependencies import admin_state, require_admin
from .mutation_audit import add_audit
from .validation import json_object

router = APIRouter(prefix="/settings")

SETTING_SCHEMA: dict[str, dict[str, Any]] = {
    "service.worker.autostart": {
        "default": False, "apply_mode": "control_restart_required",
        "restart_required": True, "type": bool,
    },
    "service.worker.start_timeout_seconds": {
        "default": 30, "apply_mode": "worker_restart",
        "restart_required": False, "type": int,
    },
    "checkin.enabled": {"default": False, "apply_mode": "scheduler_reschedule", "type": bool},
    "checkin.at": {"default": "00:10", "apply_mode": "scheduler_reschedule", "type": str},
    "checkin.timezone": {"default": "Asia/Shanghai", "apply_mode": "scheduler_reschedule", "type": str},
    "checkin.catch_up": {"default": True, "apply_mode": "scheduler_reschedule", "type": bool},
    "checkin.catch_up_window_hours": {"default": 6, "apply_mode": "scheduler_reschedule", "type": int},
    "checkin.jitter_min_seconds": {"default": 3, "apply_mode": "scheduler_reschedule", "type": int},
    "checkin.jitter_max_seconds": {"default": 10, "apply_mode": "scheduler_reschedule", "type": int},
    "checkin.retry_limit": {"default": 2, "apply_mode": "immediate", "type": int},
    "monitoring.metrics_enabled": {"default": True, "apply_mode": "immediate", "type": bool},
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
        values.append(_setting_view(item, key, definition))
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
        _validate_scheduler_candidate(runtime, key, value)
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
        application = await _apply_runtime(request, key, value)
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
        status=application["status"],
        error_code=application.get("error_code"),
        metadata={key: value, **{name: value for name, value in application.items() if name != "status"}},
    )
    return {
        "key": key,
        "value": value,
        "value_version": version,
        "apply_mode": definition["apply_mode"],
        "apply_status": application["status"],
        **{name: value for name, value in application.items() if name != "status"},
    }


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
    metadata: dict[str, Any] | None = None,
) -> None:
    result = "failed" if error_code or status == "failed" else "succeeded"
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
            metadata={"apply_status": status, **(metadata or {}), **({"error_code": error_code} if error_code else {})},
        )


async def _apply_runtime(request: Request, key: str, value: Any) -> dict[str, Any]:
    result = await _runtime(request).apply_setting(key, value)
    return result if isinstance(result, dict) else {"status": str(result)}


def _runtime(request: Request):
    runtime = getattr(admin_state(request), "runtime", None)
    if runtime is None:
        raise RuntimeError("runtime_unavailable")
    return runtime


def _validate_scheduler_candidate(runtime: Any, key: str, value: Any) -> None:
    if key.startswith("checkin."):
        ScheduleConfiguration.from_settings(
            runtime.settings, {SettingsApplier.attribute(key): value}
        )


def _default_item(key: str, definition: dict[str, Any]) -> dict[str, Any]:
    return {"key": key, "value": definition["default"], "value_version": 0,
            "source": "default", "apply_mode": definition["apply_mode"], "apply_status": "effective",
            "restart_required": definition.get("restart_required", False)}


def _setting_view(
    item: dict[str, Any] | None, key: str, definition: dict[str, Any]
) -> dict[str, Any]:
    result = dict(item) if item is not None else _default_item(key, definition)
    result["apply_mode"] = definition["apply_mode"]
    result["restart_required"] = definition.get("restart_required", False)
    return result


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
