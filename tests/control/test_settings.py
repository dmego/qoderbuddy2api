"""Durable runtime settings application tests."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from qb2api.accounts.repository import AccountRepository
from qb2api.config import Settings
from qb2api.runtime import RuntimeServices


@pytest.mark.asyncio
async def test_runtime_loads_effective_settings_from_database(tmp_path):
    path = tmp_path / "qb2api.sqlite3"
    repository = AccountRepository(str(path))
    await repository.connect()
    await repository.migrate()
    await repository.upsert_runtime_setting(
        key="monitoring.metrics_interval_seconds",
        value=120,
        expected_version=0,
        apply_mode="immediate",
    )
    await repository.close()

    settings = Settings(
        admin_key="admin-secret",
        credential_key=Fernet.generate_key().decode(),
        data_dir=str(tmp_path),
        metrics_enabled=False,
    )
    runtime = await RuntimeServices.start(settings)
    try:
        assert runtime.settings.metrics_interval_seconds == 120
        stored = await runtime.account_repo.get_runtime_setting(
            "monitoring.metrics_interval_seconds"
        )
        assert stored["apply_status"] == "effective"
    finally:
        await runtime.close()
