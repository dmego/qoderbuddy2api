"""Manual check-in and run history routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from qb2api.checkin.service import CheckinInProgressError, CheckinTarget

from .dependencies import admin_state, require_admin
from .validation import json_object, required_string

router = APIRouter()


@router.get("/checkin/status")
async def checkin_status(request: Request) -> dict[str, Any]:
    await require_admin(request)
    state = admin_state(request)
    return await state.checkin_service.status_snapshot(next_run_at=_next_run(state))


@router.post("/checkin/run")
async def checkin_run(request: Request) -> Any:
    await require_admin(request)
    state = admin_state(request)
    body = await json_object(request, allow_empty=True)
    targets = _targets(body)
    try:
        batch = await state.checkin_service.run_batch(
            trigger="manual",
            targets=targets,
            skip_already_done=False,
        )
    except CheckinInProgressError:
        return JSONResponse(status_code=409, content={"error": "checkin_run_in_progress"})
    return {
        "run_id": batch.run_id,
        "status": batch.status,
        "local_date": batch.local_date,
        "timezone": batch.timezone,
        "results": batch.results,
    }


@router.get("/checkin/runs")
async def checkin_runs(request: Request, limit: int = 20) -> dict[str, Any]:
    await require_admin(request)
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=400, detail="invalid_limit")
    runs = await admin_state(request).account_repo.list_checkin_runs(limit)
    return {"runs": runs, "limit": limit}


@router.get("/checkin/runs/{run_id}")
async def checkin_run_detail(run_id: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    repository = admin_state(request).account_repo
    run = await repository.get_checkin_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    return {
        "run": run,
        "attempts": await repository.list_checkin_attempts(run_id),
    }


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
    scheduler = state.checkin_scheduler
    next_run = scheduler.next_run_at if scheduler is not None else None
    return next_run.isoformat() if next_run is not None else None
