"""Admin API for Proxy Worker lifecycle operations."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from qb2api.admin.dependencies import require_admin
from qb2api.admin.validation import bounded_int, choice_filter, cursor_value

from .service_models import SupervisorOperation
from .supervisor import ServiceSupervisor

router = APIRouter(prefix="/api/admin/service", tags=["service"])


@router.get("")
async def get_service(request: Request) -> dict[str, Any]:
    await require_admin(request)
    supervisor = _supervisor(request)
    await supervisor.reconcile()
    return _snapshot_view(supervisor)


@router.get("/operations/{operation_id}")
async def get_operation(operation_id: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    operation = _supervisor(request).operation(operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="operation_not_found")
    return _operation_view(operation)


@router.get("/events")
async def get_service_events(
    request: Request,
    *,
    cursor: str | None = None,
    limit: str | None = None,
    event_type: str | None = None,
    status: str | None = None,
    result: str | None = None,
) -> dict[str, Any]:
    await require_admin(request)
    repository = getattr(request.app.state, "account_repo", None)
    if repository is None:
        raise HTTPException(status_code=503, detail="repository_unavailable")
    selected_limit = bounded_int(limit, default=50, maximum=100)
    selected_type = choice_filter(
        event_type, {"state", "operation"}, detail="invalid_event_type"
    )
    selected_status = _status_filter(status, result)
    events, next_cursor = await repository.list_service_events(
        cursor=cursor_value(cursor),
        limit=selected_limit,
        event_type=selected_type,
        status=selected_status,
    )
    return {
        "events": events,
        "limit": selected_limit,
        "next_cursor": next_cursor,
    }


@router.post("/{action}")
async def mutate_service(action: str, request: Request) -> JSONResponse:
    await require_admin(request)
    if action not in {"start", "stop", "restart", "reload"}:
        raise HTTPException(status_code=404, detail="unknown_service_action")
    supervisor = _supervisor(request)
    method = getattr(supervisor, action)
    operation = await method(idempotency_key=request.headers.get("Idempotency-Key"))
    status_code = 202 if operation.status == "running" else 200
    return JSONResponse(status_code=status_code, content=_operation_view(operation))


def _supervisor(request: Request) -> ServiceSupervisor:
    supervisor = getattr(request.app.state, "supervisor", None)
    if supervisor is None:
        raise HTTPException(status_code=503, detail="supervisor_unavailable")
    return supervisor


def _snapshot_view(supervisor: ServiceSupervisor) -> dict[str, Any]:
    snapshot = asdict(supervisor.snapshot)
    return {"service": "proxy-worker", **snapshot}


def _operation_view(operation: SupervisorOperation) -> dict[str, Any]:
    return {
        "operation_id": operation.operation_id,
        "action": operation.action,
        "status": operation.status,
        "error_code": operation.error,
        "in_flight": operation.in_flight,
        "created_at": operation.created_at,
        "finished_at": operation.finished_at,
    }


def _status_filter(status: str | None, result: str | None) -> str | None:
    if status is not None and result is not None and status != result:
        raise HTTPException(status_code=400, detail="conflicting_status_filter")
    return choice_filter(
        status or result,
        {"running", "succeeded", "failed", "cancelled"},
        detail="invalid_status",
    )
