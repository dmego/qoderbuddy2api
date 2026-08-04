"""Manual check-in and run history routes."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from qb2api.checkin.service import CheckinInProgressError, CheckinTarget

from .dependencies import admin_state, require_admin
from .mutation_audit import add_audit
from .validation import bounded_int, choice_filter, json_object, required_string

router = APIRouter()


@router.get("/checkin/status")
async def checkin_status(request: Request) -> dict[str, Any]:
    await require_admin(request)
    state = admin_state(request)
    snapshot = await state.checkin_service.status_snapshot(next_run_at=_next_run(state))
    scheduler = getattr(state, "checkin_scheduler", None)
    if scheduler is not None:
        snapshot["scheduler"] = scheduler.status_snapshot()
        snapshot["next_run_at"] = snapshot["scheduler"]["next_run_at"]
    metrics = getattr(state, "metrics_scheduler", None)
    if metrics is not None:
        snapshot["metrics"] = metrics.status_snapshot()
    snapshot["daily_states"] = [
        _daily_state_view(item) for item in snapshot.get("daily_states", [])
    ]
    return snapshot


@router.post("/checkin/run")
async def checkin_run(request: Request) -> Any:
    await require_admin(request)
    state = admin_state(request)
    body = await json_object(request, allow_empty=True)
    targets = _targets(body)
    try:
        run_id = await state.checkin_service.start_batch(
            trigger="manual",
            targets=targets,
            skip_already_done=False,
        )
    except CheckinInProgressError:
        await add_audit(
            state.account_repo,
            action="checkin.run",
            resource_type="checkin",
            resource_id=None,
            result="failed",
            metadata={
                "error_code": "checkin_run_in_progress",
                "trigger": "manual",
            },
        )
        return JSONResponse(status_code=409, content={"error": "checkin_run_in_progress"})
    await add_audit(
        state.account_repo,
        action="checkin.run",
        resource_type="checkin",
        resource_id=run_id,
        result="running",
        metadata={"operation_id": run_id, "trigger": "manual"},
    )
    return JSONResponse(
        status_code=202,
        content={"operation_id": run_id, "run_id": run_id, "status": "running"},
    )


@router.get("/checkin/runs")
async def checkin_runs(
    request: Request,
    *,
    limit: str | None = None,
    cursor: str | None = None,
    status: str | None = None,
    trigger: str | None = None,
) -> dict[str, Any]:
    await require_admin(request)
    selected_limit = bounded_int(limit, default=20, maximum=100)
    selected_status = choice_filter(
        status,
        {"running", "finished", "failed", "cancelled"},
        detail="invalid_status",
    )
    selected_trigger = _trigger_filter(trigger)
    runs, next_key = await admin_state(request).account_repo.list_checkin_runs_page(
        limit=selected_limit,
        cursor=_decode_cursor(cursor),
        status=selected_status,
        trigger=selected_trigger,
    )
    return {
        "runs": runs,
        "limit": selected_limit,
        "next_cursor": _encode_cursor(next_key),
    }


@router.get("/checkin/runs/{run_id}")
async def checkin_run_detail(run_id: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    repository = admin_state(request).account_repo
    run = await repository.get_checkin_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    return {
        "run": run,
        "attempts": [
            _attempt_view(item)
            for item in await repository.list_checkin_attempts(run_id)
        ],
    }


def _daily_state_view(item: dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    result["terminal_outcome"] = _lower_value(result.get("terminal_outcome"))
    return result


def _attempt_view(item: dict[str, Any]) -> dict[str, Any]:
    error_code = item.get("business_code")
    if error_code is None and item.get("redacted_error"):
        error_code = "checkin_failed"
    return {
        "provider": item.get("provider"),
        "account_id": item.get("account_id"),
        "outcome": _lower_value(item.get("outcome")),
        "http_status": item.get("http_status"),
        "attempts": item.get("attempts", 0),
        "finished_at": item.get("finished_at"),
        "error_code": str(error_code) if error_code is not None else None,
        "reward_credits": item.get("reward_credits"),
        "reward_expires_at": item.get("reward_expires_at"),
        "quota_before": item.get("quota_before"),
        "quota_after": item.get("quota_after"),
        "quota_delta": item.get("quota_delta"),
        "quota_observed_at": item.get("quota_observed_at"),
        "quota_change_status": item.get("quota_change_status"),
    }


def _lower_value(value: Any) -> str | None:
    return value.lower() if isinstance(value, str) else None


def _targets(body: dict[str, Any]) -> list[CheckinTarget] | None:
    raw = body.get("targets")
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="invalid_targets")
    targets = []
    for item in raw:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="invalid_target")
        provider = required_string(item, "provider", detail="invalid_target")
        account_id = required_string(item, "account_id", detail="invalid_target")
        if provider not in {"codebuddy", "qoder"}:
            raise HTTPException(status_code=400, detail="invalid_target")
        targets.append(CheckinTarget(provider=provider, account_id=account_id))
    return targets


def _next_run(state: Any) -> str | None:
    scheduler = getattr(state, "checkin_scheduler", None)
    next_run = scheduler.next_run_at if scheduler is not None else None
    return next_run.isoformat() if next_run is not None else None


def _trigger_filter(value: str | None) -> str | None:
    normalized = "scheduler" if value == "scheduled" else value
    return choice_filter(
        normalized,
        {"manual", "scheduler", "catch_up", "verify"},
        detail="invalid_trigger",
    )


def _decode_cursor(value: str | None) -> tuple[str, str] | None:
    if value is None:
        return None
    if len(value) > 512:
        raise HTTPException(status_code=400, detail="invalid_cursor")
    try:
        padding = "=" * (-len(value) % 4)
        payload = base64.urlsafe_b64decode(f"{value}{padding}")
        decoded = json.loads(payload)
    except (ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=400, detail="invalid_cursor") from error
    if not _valid_cursor_payload(decoded):
        raise HTTPException(status_code=400, detail="invalid_cursor")
    return decoded[0], decoded[1]


def _encode_cursor(value: tuple[str, str] | None) -> str | None:
    if value is None:
        return None
    payload = json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _valid_cursor_payload(value: Any) -> bool:
    if not isinstance(value, list) or len(value) != 2:
        return False
    started_at, run_id = value
    if not isinstance(started_at, str) or not isinstance(run_id, str):
        return False
    if not 1 <= len(started_at) <= 64 or not 1 <= len(run_id) <= 128:
        return False
    try:
        datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True
