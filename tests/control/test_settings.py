"""Durable runtime settings application tests."""

from __future__ import annotations

import stat

import pytest
from cryptography.fernet import Fernet

from qb2api.accounts.repository import AccountRepository
from qb2api.config import Settings
from qb2api.runtime import RuntimeServices


def test_history_retention_setting_validation():
    from qb2api.control.settings import SettingsApplier

    assert (
        SettingsApplier.attribute("monitoring.metrics_history_retention_days")
        == "metrics_history_retention_days"
    )
    with pytest.raises(ValueError):
        SettingsApplier.validate("monitoring.metrics_history_retention_days", 0)


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


@pytest.mark.asyncio
async def test_durable_runtime_restricts_existing_data_directory(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)
    data_dir.chmod(0o755)
    settings = Settings(
        admin_key="admin-secret",
        credential_key=Fernet.generate_key().decode(),
        data_dir=str(data_dir),
        metrics_enabled=False,
    )

    runtime = await RuntimeServices.start(settings)
    try:
        assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    finally:
        await runtime.close()
