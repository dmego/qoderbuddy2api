"""Usage, account metrics, and audit-facing read APIs."""

from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from .dependencies import admin_state, require_admin

router = APIRouter()


@router.get("/usage/summary")
async def usage_summary(request: Request) -> dict[str, Any]:
    await require_admin(request)
    return {"summary": await _repository(request).usage_summary(**_filters(request))}


@router.get("/usage/events")
async def usage_events(
    request: Request, limit: int = Query(100, ge=1, le=500)
) -> dict[str, Any]:
    await require_admin(request)
    events = await _repository(request).list_request_events(limit=limit, **_filters(request))
    return {"events": events}


@router.get("/usage/events/{event_id}")
async def usage_event(event_id: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    event = await _repository(request).get_request_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="usage_event_not_found")
    return _safe_event(event)


@router.get("/usage/rollups")
async def usage_rollups(
    request: Request,
    bucket_kind: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    await require_admin(request)
    if bucket_kind not in {None, "minute", "day", "month"}:
        raise HTTPException(status_code=400, detail="invalid_bucket_kind")
    rollups = await _repository(request).list_usage_rollups(
        bucket_kind=bucket_kind, limit=limit, **_filters(request)
    )
    return {"rollups": rollups}


@router.get("/usage/timeseries")
async def usage_timeseries(
    request: Request,
    bucket_kind: str = Query("minute"),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    if bucket_kind not in {"minute", "day", "month"}:
        raise HTTPException(status_code=400, detail="invalid_bucket_kind")
    await require_admin(request)
    rollups = await _repository(request).list_usage_rollups(
        bucket_kind=bucket_kind, limit=limit, **_filters(request)
    )
    return {"rollups": rollups, "bucket_kind": bucket_kind, "limit": limit}


@router.get("/usage/export")
async def usage_export(
    request: Request, limit: int = Query(500, ge=1, le=500)
) -> Response:
    await require_admin(request)
    filters = _filters(request)
    events = await _repository(request).list_request_events(limit=limit, **filters)
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    fields = (
        "event_id", "request_id", "provider", "account_id", "model_id", "protocol",
        "status", "http_status", "input_tokens", "output_tokens", "latency_ms",
        "stream_committed", "started_at", "finished_at", "error_code",
    )
    writer.writerow(fields)
    for event in events:
        writer.writerow([event.get(field) for field in fields])
    await _repository(request).add_audit_event(
        actor_type="admin", actor_id=None, action="usage.export",
        resource_type="usage", resource_id=None, result="succeeded", metadata=filters,
    )
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=usage-events.csv"},
    )


@router.post("/usage/rollup")
async def refresh_rollups(request: Request) -> dict[str, Any]:
    await require_admin(request)
    service = getattr(admin_state(request), "usage_rollup_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="usage_rollup_unavailable")
    return {"status": await service.rollup_once()}


@router.get("/metrics/accounts")
async def account_metrics(request: Request, provider: str | None = None) -> dict[str, Any]:
    await require_admin(request)
    snapshots = await _repository(request).list_metric_snapshots(provider)
    return {"snapshots": snapshots}


@router.post("/metrics/refresh")
async def refresh_metrics(request: Request) -> dict[str, Any]:
    await require_admin(request)
    scheduler = getattr(admin_state(request), "metrics_scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="metrics_scheduler_unavailable")
    operation = await scheduler.refresh_once()
    return {"status": operation}


@router.get("/audit")
async def audit_events(request: Request, limit: int = 100) -> dict[str, Any]:
    await require_admin(request)
    return {"events": await _repository(request).list_audit_events(limit)}


def _repository(request: Request):
    repository = getattr(admin_state(request), "account_repo", None)
    if repository is None:
        raise HTTPException(status_code=503, detail="repository_unavailable")
    return repository


def _filters(request: Request) -> dict[str, str | None]:
    return {
        "provider": request.query_params.get("provider"),
        "account_id": request.query_params.get("account_id"),
        "model_id": request.query_params.get("model_id"),
        "started_after": request.query_params.get("started_after"),
        "started_before": request.query_params.get("started_before"),
    }


def _safe_event(event: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "event_id", "request_id", "provider", "account_id", "model_id", "protocol",
        "status", "http_status", "input_tokens", "output_tokens", "latency_ms",
        "stream_committed", "started_at", "finished_at", "error_code",
    )
    return {field: event.get(field) for field in fields}
