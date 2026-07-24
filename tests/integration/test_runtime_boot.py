"""Runtime assembly contracts for env-only and durable modes."""

from __future__ import annotations

import asyncio

import pytest
from cryptography.fernet import Fernet

from qb2api.accounts.repository import AccountRepository
from qb2api.config import Settings
from qb2api.providers import ProviderRegistry
from qb2api.runtime import RuntimeServices
from qb2api.worker.runtime import WorkerRuntime, local_snapshot


class _BlockingRefresh:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def refresh_once(self) -> dict[str, int]:
        self.started.set()
        await asyncio.Event().wait()
        return {}


@pytest.mark.asyncio
async def test_env_only_worker_runtime_registers_stable_pools_without_storage() -> None:
    settings = Settings(
        codebuddy_tokens=["ck-env"],
        qoder_tokens=["pt-env"],
    )

    runtime = WorkerRuntime(settings, ProviderRegistry())
    await runtime.start(local_snapshot(settings))
    try:
        assert runtime.codebuddy_pool.instance_count == 1
        assert runtime.qoder_pool.instance_count == 1
        assert runtime.providers.get("codebuddy") is runtime.codebuddy_pool
        assert runtime.providers.get("qoder") is runtime.qoder_pool
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_durable_runtime_owns_storage_and_keeps_pool_identity(tmp_path) -> None:
    settings = Settings(
        admin_key="admin-secret",
        credential_key=Fernet.generate_key().decode(),
        data_dir=str(tmp_path),
        codebuddy_tokens=[],
        qoder_tokens=[],
    )

    runtime = await RuntimeServices.start(settings)
    registry = runtime.account_registry
    try:
        assert runtime.account_repo is not None
        assert runtime.account_registry is not None
        assert runtime.credential_resolver is not None
        await runtime.refresh_accounts()
        assert runtime.account_registry is registry
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_rejects_admin_mode_without_required_keys(tmp_path) -> None:
    settings = Settings(admin_ui_enabled=True, data_dir=str(tmp_path))

    with pytest.raises(ValueError, match="QB2API_ADMIN_KEY"):
        await RuntimeServices.start(settings)


@pytest.mark.asyncio
async def test_runtime_recovers_interrupted_metric_refresh(tmp_path) -> None:
    settings = Settings(
        admin_key="admin-secret",
        credential_key=Fernet.generate_key().decode(),
        data_dir=str(tmp_path),
    )
    repository = AccountRepository(str(tmp_path / "qb2api.sqlite3"))
    await repository.connect()
    await repository.migrate()
    operation_id = await repository.create_metric_refresh_operation()
    await repository.close()

    runtime = await RuntimeServices.start(settings)
    try:
        operation = await runtime.account_repo.get_metric_refresh_operation(operation_id)
        assert operation["status"] == "cancelled"
        assert operation["error_code"] == "refresh_interrupted"
        audit = await runtime.account_repo.list_audit_events()
        recovered = [event for event in audit if event["resource_id"] == operation_id]
        assert recovered[0]["result"] == "cancelled"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_close_cancels_refresh_wrappers_before_repository(tmp_path) -> None:
    settings = Settings(
        admin_key="admin-secret",
        credential_key=Fernet.generate_key().decode(),
        data_dir=str(tmp_path),
    )
    runtime = await RuntimeServices.start(settings)
    operation_id = await runtime.account_repo.create_metric_refresh_operation()
    scheduler = _BlockingRefresh()
    task = asyncio.create_task(
        runtime.account_repo.run_metric_refresh_operation(operation_id, scheduler)
    )
    runtime.metrics_refresh_tasks.add(task)
    task.add_done_callback(runtime.metrics_refresh_tasks.discard)
    await scheduler.started.wait()
    await runtime.close()

    repository = AccountRepository(str(tmp_path / "qb2api.sqlite3"))
    await repository.connect()
    try:
        operation = await repository.get_metric_refresh_operation(operation_id)
        assert operation["status"] == "cancelled"
        assert operation["error_code"] == "refresh_cancelled"
    finally:
        await repository.close()
