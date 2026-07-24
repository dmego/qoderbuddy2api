"""Backup integrity and non-destructive restore validation."""

from __future__ import annotations

import pytest

from qb2api.accounts.repository import AccountRepository
from qb2api.admin.backup import BackupError, BackupService


@pytest.mark.asyncio
async def test_backup_roundtrip_and_restore_dry_run(tmp_path):
    repository = AccountRepository(str(tmp_path / "qb2api.sqlite3"))
    await repository.connect()
    await repository.migrate()
    await repository.upsert_account(
        provider="codebuddy",
        account_id="cb-1",
        label="main",
        source="manual",
        enabled=True,
    )
    service = BackupService(data_dir=str(tmp_path), repository=repository)
    result = await service.create()
    assert result["status"] == "succeeded"
    assert len(result["sha256"]) == 64
    checked = await service.validate_restore(result["backup_id"])
    assert checked["valid"] is True
    assert checked["dry_run"] is True
    await repository.close()


@pytest.mark.asyncio
async def test_backup_checksum_tamper_is_rejected(tmp_path):
    repository = AccountRepository(str(tmp_path / "qb2api.sqlite3"))
    await repository.connect()
    await repository.migrate()
    service = BackupService(data_dir=str(tmp_path), repository=repository)
    result = await service.create()
    row = await service.get(result["backup_id"])
    with open(row["path"], "ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(BackupError, match="checksum"):
        await service.validate_restore(result["backup_id"])
    await repository.close()
