"""Usage, account metrics, and audit-facing read APIs."""

from __future__ import annotations

import asyncio
import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .dependencies import admin_state, require_admin
from .observability_support import (
    audit_action_filters,
    audit_search_filter,
)
from .observability_support import (
    page as _page,
)
from .observability_support import (
    repository as _repository,
)
from .observability_support import (
    safe_event as _safe_event,
)
from .observability_support import (
    track_task as _track_task,
)
from .observability_support import (
    usage_filters as _filters,
)
from .validation import (
    bounded_int,
    cursor_value,
    optional_account_id,
    page_slice,
    provider_filter,
    text_filter,
    time_range,
)
from .views import find_account_view

router = APIRouter()


@router.get("/usage/summary")
async def usage_summary(request: Request) -> dict[str, Any]:
    await require_admin(request)
    return {"summary": await _repository(request).usage_summary(**_filters(request))}


@router.get("/usage/events")
async def usage_events(
    request: Request,
    limit: str | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    await require_admin(request)
    selected_limit = bounded_int(limit, default=100, maximum=500)
    offset = cursor_value(cursor, allow_zero=True) or 0
    events = await _repository(request).list_request_events(
        limit=selected_limit + 1,
        offset=offset,
        **_filters(request),
    )
    page = events[:selected_limit]
    return {
        "events": page,
        "limit": selected_limit,
        "next_cursor": offset + selected_limit if len(events) > selected_limit else None,
    }


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
    limit: str | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    await require_admin(request)
    if bucket_kind not in {None, "minute", "day", "month"}:
        raise HTTPException(status_code=400, detail="invalid_bucket_kind")
    selected_limit = bounded_int(limit, default=100, maximum=500)
    offset = cursor_value(cursor, allow_zero=True) or 0
    rollups = await _repository(request).list_usage_rollups(
        bucket_kind=bucket_kind,
        limit=selected_limit + 1,
        offset=offset,
        **_filters(request),
    )
    return _page("rollups", rollups, selected_limit, offset)


@router.get("/usage/timeseries")
async def usage_timeseries(
    request: Request,
    bucket_kind: str = "minute",
    limit: str | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    if bucket_kind not in {"minute", "day", "month"}:
        raise HTTPException(status_code=400, detail="invalid_bucket_kind")
    await require_admin(request)
    selected_limit = bounded_int(limit, default=100, maximum=500)
    offset = cursor_value(cursor, allow_zero=True) or 0
    rollups = await _repository(request).list_usage_rollups(
        bucket_kind=bucket_kind,
        limit=selected_limit + 1,
        offset=offset,
        **_filters(request),
    )
    result = _page("rollups", rollups, selected_limit, offset)
    result["bucket_kind"] = bucket_kind
    return result


@router.get("/usage/export")
async def usage_export(
    request: Request, limit: str | None = None
) -> Response:
    await require_admin(request)
    selected_limit = bounded_int(limit, default=500, maximum=500)
    filters = _filters(request)
    events = await _repository(request).list_request_events(limit=selected_limit, **filters)
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
    result = await service.rollup_once()
    await _repository(request).add_audit_event(
        actor_type="admin",
        actor_id=None,
        action="usage.rollup",
        resource_type="usage",
        resource_id=None,
        result="succeeded",
    )
    return {"status": result}


@router.get("/metrics/accounts")
async def account_metrics(
    request: Request,
    provider: str | None = None,
    cursor: str | None = None,
    limit: str | None = None,
) -> dict[str, Any]:
    await require_admin(request)
    snapshots = await _repository(request).list_metric_snapshots(provider_filter(provider))
    selected_limit = bounded_int(limit, default=100, maximum=500)
    page, next_cursor = page_slice(
        snapshots,
        cursor_value(cursor, allow_zero=True),
        selected_limit,
    )
    return {"snapshots": page, "limit": selected_limit, "next_cursor": next_cursor}


@router.get("/metrics/accounts/{provider}/{account_id}")
async def account_metric_detail(
    provider: str,
    account_id: str,
    request: Request,
) -> dict[str, Any]:
    await require_admin(request)
    selected_provider = provider_filter(provider)
    selected_account = optional_account_id(account_id)
    state = admin_state(request)
    if find_account_view(state, selected_provider, selected_account) is None:
        raise HTTPException(status_code=404, detail="account_not_found")
    snapshots = await _repository(request).list_metric_snapshots(
        selected_provider,
        selected_account,
    )
    return {
        "provider": selected_provider,
        "account_id": selected_account,
        "snapshots": snapshots,
    }


@router.post("/metrics/refresh")
async def refresh_metrics(request: Request) -> JSONResponse:
    await require_admin(request)
    scheduler = getattr(admin_state(request), "metrics_scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="metrics_scheduler_unavailable")
    repository = _repository(request)
    operation_id = await repository.create_metric_refresh_operation()
    task = asyncio.create_task(
        repository.run_metric_refresh_operation(operation_id, scheduler),
        name=f"qb2api-metrics-admin-{operation_id}",
    )
    _track_task(request.app, task)
    return JSONResponse(
        status_code=202,
        content={"operation_id": operation_id, "status": "running"},
    )


@router.get("/metrics/refresh/{operation_id}")
async def metric_refresh_result(operation_id: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    operation = await _repository(request).get_metric_refresh_operation(operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="metrics_refresh_not_found")
    return operation


@router.get("/audit")
async def audit_events(
    request: Request,
    *,
    limit: str | None = None,
    cursor: str | None = None,
    action: str | None = None,
    action_prefix: str | None = None,
    category: str | None = None,
    search: str | None = None,
    query: str | None = None,
    resource_type: str | None = None,
    result: str | None = None,
    started_after: str | None = None,
    started_before: str | None = None,
) -> dict[str, Any]:
    await require_admin(request)
    selected_limit = bounded_int(limit, default=100, maximum=500)
    offset = cursor_value(cursor, allow_zero=True) or 0
    after, before = time_range(started_after, started_before)
    selected_action, selected_prefix = audit_action_filters(
        action,
        action_prefix,
        category,
    )
    events, next_cursor = await _repository(request).list_audit_events_page(
        limit=selected_limit,
        offset=offset,
        action=selected_action,
        action_prefix=selected_prefix,
        search=audit_search_filter(search, query),
        resource_type=text_filter(resource_type, detail="invalid_resource_type"),
        result=text_filter(result, detail="invalid_result"),
        started_after=after,
        started_before=before,
    )
    return {"events": events, "limit": selected_limit, "next_cursor": next_cursor}
