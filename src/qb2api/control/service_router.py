"""Admin API for Proxy Worker lifecycle operations."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from qb2api.admin.dependencies import require_admin

from .service_models import SupervisorOperation
from .supervisor import ServiceSupervisor

router = APIRouter(prefix="/api/admin/service", tags=["service"])


@router.get("")
async def get_service(request: Request) -> dict[str, Any]:
    await require_admin(request)
    supervisor = _supervisor(request)
    await supervisor.reconcile()
    return _snapshot_view(supervisor)


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


@router.get("/operations/{operation_id}")
async def get_operation(operation_id: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    operation = _supervisor(request).operation(operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="operation_not_found")
    return _operation_view(operation)


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
        "error": operation.error,
        "created_at": operation.created_at,
        "finished_at": operation.finished_at,
    }
