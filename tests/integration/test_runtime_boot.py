"""Runtime assembly contracts for env-only and durable modes."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from qb2api.config import Settings
from qb2api.providers import ProviderRegistry
from qb2api.runtime import RuntimeServices
from qb2api.worker.runtime import WorkerRuntime, local_snapshot


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
