"""Control Plane runtime lifecycle wiring."""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from qb2api.config import Settings
from qb2api.runtime import RuntimeServices
from qb2api.storage_permissions import ensure_private_directory

from .runtime_snapshot import RuntimeSnapshotService
from .service_models import ServiceSnapshot
from .supervisor import ServiceSupervisor


@dataclass(slots=True)
class _ControlContext:
    runtime: RuntimeServices
    supervisor: ServiceSupervisor
    autostart: asyncio.Task[Any] | None


@asynccontextmanager
async def control_lifespan(application: FastAPI) -> AsyncIterator[None]:
    context = await _start_control(application)
    try:
        yield
    finally:
        await _stop_control(context)


async def _start_control(application: FastAPI) -> _ControlContext:
    settings = _ensure_internal_token(application.state.settings_factory())
    application.state.settings = settings
    runtime = await RuntimeServices.start(settings)
    runtime.attach(application)
    supervisor = application.state.supervisor_factory(
        settings,
        state_writer=_state_writer(runtime.account_repo),
        operation_writer=_operation_writer(runtime.account_repo),
    )
    snapshot_service = RuntimeSnapshotService(runtime)
    application.state.supervisor = supervisor
    application.state.runtime_snapshot_service = snapshot_service
    _bind_runtime_callbacks(
        application=application,
        runtime=runtime,
        supervisor=supervisor,
        snapshot_service=snapshot_service,
    )
    if runtime.backup_service is not None:
        await runtime.backup_service.recover_interrupted()
    await _restore_supervisor(runtime=runtime, supervisor=supervisor)
    autostart = _start_worker(application, settings=settings, supervisor=supervisor)
    return _ControlContext(runtime=runtime, supervisor=supervisor, autostart=autostart)


def _bind_runtime_callbacks(
    *,
    application: FastAPI,
    runtime: RuntimeServices,
    supervisor: ServiceSupervisor,
    snapshot_service: RuntimeSnapshotService,
) -> None:
    runtime.worker_settings_apply = partial(
        _apply_worker_setting,
        supervisor=supervisor,
        snapshot_service=snapshot_service,
    )
    application.state.refresh_provider_pools = partial(
        _refresh_runtime,
        runtime=runtime,
        supervisor=supervisor,
        snapshot_service=snapshot_service,
    )


async def _apply_worker_setting(
    action: str,
    *,
    supervisor: ServiceSupervisor,
    snapshot_service: RuntimeSnapshotService,
) -> dict[str, Any]:
    if supervisor.snapshot.desired_state != "RUNNING":
        return {"status": "effective", "restart_required": False}
    operation = await getattr(supervisor, action)(
        idempotency_key=f"runtime-setting-{action}-{snapshot_service.version}"
    )
    if operation.status == "succeeded":
        return {
            "status": "effective",
            "operation_id": operation.operation_id,
            "restart_required": False,
        }
    return {
        "status": "failed",
        "operation_id": operation.operation_id,
        "error_code": operation.error or "service_operation_failed",
    }


async def _refresh_runtime(
    *,
    runtime: RuntimeServices,
    supervisor: ServiceSupervisor,
    snapshot_service: RuntimeSnapshotService,
) -> None:
    await runtime.refresh_accounts()
    snapshot_service.bump()
    if supervisor.snapshot.desired_state != "RUNNING":
        return
    operation = await supervisor.reload(
        idempotency_key=f"runtime-snapshot-{snapshot_service.version}"
    )
    if operation.status != "succeeded":
        raise RuntimeError(operation.error or "worker runtime reload failed")


async def _restore_supervisor(
    *, runtime: RuntimeServices, supervisor: ServiceSupervisor
) -> None:
    if runtime.account_repo is None:
        return
    saved = await runtime.account_repo.get_service_runtime("proxy-worker")
    await supervisor.restore(saved)


def _start_worker(
    application: FastAPI,
    *,
    settings: Settings,
    supervisor: ServiceSupervisor,
) -> asyncio.Task[Any] | None:
    if not settings.worker_autostart:
        return None
    task = asyncio.create_task(
        supervisor.start(idempotency_key="control-plane-autostart")
    )
    application.state.autostart_task = task
    return task


async def _stop_control(context: _ControlContext) -> None:
    if context.autostart is not None and not context.autostart.done():
        await context.autostart
    await context.supervisor.stop(idempotency_key="control-plane-shutdown")
    await context.runtime.close()


def _ensure_internal_token(settings: Settings) -> Settings:
    if settings.worker_internal_token:
        return settings
    data_dir = ensure_private_directory(settings.data_dir)
    token_path = data_dir / "worker.internal"
    token = _read_or_create_token(token_path)
    if not token:
        raise RuntimeError("worker internal token file is empty")
    token_path.chmod(0o600)
    return replace(settings, worker_internal_token=token)


def _read_or_create_token(token_path: Path) -> str:
    if token_path.exists():
        return token_path.read_text().strip()
    token = secrets.token_urlsafe(32)
    descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(token)
    return token


def _state_writer(repository: Any):
    async def write(snapshot: ServiceSnapshot) -> None:
        if repository is not None:
            await repository.save_service_runtime(
                "proxy-worker", _snapshot_values(snapshot)
            )

    return write


def _operation_writer(repository: Any):
    async def write(operation: Any) -> None:
        if repository is not None:
            await repository.save_service_operation(operation)

    return write


def _snapshot_values(snapshot: ServiceSnapshot) -> dict[str, Any]:
    identity = snapshot.identity
    return {
        "desired_state": snapshot.desired_state,
        "observed_state": snapshot.observed_state,
        "worker_pid": identity.pid if identity else None,
        "process_start_time": identity.process_start_time if identity else None,
        "process_group_id": identity.process_group_id if identity else None,
        "owner_instance_id": identity.owner_instance_id if identity else None,
        "internal_auth_version": identity.internal_auth_version if identity else None,
        "started_at": _iso(snapshot.started_at),
        "stopped_at": _iso(snapshot.stopped_at),
        "last_health_at": _iso(snapshot.last_health_at),
        "last_exit_code": snapshot.last_exit_code,
        "last_error": snapshot.last_error,
        "in_flight": snapshot.in_flight,
    }


def _iso(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, UTC).isoformat() if value else None
