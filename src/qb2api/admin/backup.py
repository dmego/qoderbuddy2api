"""SQLite backup creation and non-destructive restore validation."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from qb2api.accounts.repository import AccountRepository


class BackupError(RuntimeError):
    """A backup is missing, invalid, or unsafe to use."""


class BackupService:
    def __init__(self, *, data_dir: str, repository: AccountRepository) -> None:
        self.repository = repository
        self.backup_dir = (Path(data_dir) / "backups").resolve()
        self.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.backup_dir.chmod(0o700)
        self._lock = asyncio.Lock()

    async def create(self) -> dict[str, Any]:
        async with self._lock:
            backup_id = uuid.uuid4().hex
            path = self.backup_dir / f"qb2api-{backup_id}.sqlite3"
            schema_version = await self.repository.schema_version()
            await self.repository.create_backup_run(
                backup_id=backup_id,
                path=str(path),
                schema_version=schema_version,
            )
            try:
                await self.repository.backup_to(str(path))
                path.chmod(0o600)
                validation = await asyncio.to_thread(_validate_file, path)
                checksum = await asyncio.to_thread(_sha256, path)
                await self.repository.finish_backup_run(
                    backup_id,
                    status="succeeded",
                    size_bytes=path.stat().st_size,
                    sha256=checksum,
                )
            except asyncio.CancelledError:
                await asyncio.shield(
                    self.repository.finish_backup_run(
                        backup_id, status="cancelled", error_message="backup_cancelled"
                    )
                )
                raise
            except Exception as error:
                await self.repository.finish_backup_run(
                    backup_id,
                    status="failed",
                    error_message=type(error).__name__,
                )
                raise BackupError("backup creation failed") from error
            return {
                "backup_id": backup_id,
                "status": "succeeded",
                "size_bytes": path.stat().st_size,
                "sha256": checksum,
                "schema_version": validation["schema_version"],
            }

    async def recover_interrupted(self) -> list[str]:
        """Close incomplete durable records left by a Control Plane restart."""
        recovered = []
        for row in await self.repository.list_backup_runs(limit=500):
            if row.get("status") != "running":
                continue
            backup_id = str(row["backup_id"])
            await self.repository.finish_backup_run(
                backup_id, status="cancelled", error_message="backup_interrupted"
            )
            await self.repository.add_audit_event(
                actor_type="system",
                actor_id=None,
                action="backup.recover",
                resource_type="backup",
                resource_id=backup_id,
                result="cancelled",
                metadata={"error_code": "backup_interrupted"},
            )
            recovered.append(backup_id)
        return recovered

    async def get(self, backup_id: str) -> dict[str, Any]:
        row = await self.repository.get_backup_run(backup_id)
        if row is None:
            raise BackupError("backup not found")
        return row

    async def validate_restore(self, backup_id: str) -> dict[str, Any]:
        row = await self.get(backup_id)
        path = self._safe_path(row["path"])
        if row.get("status") != "succeeded" or not path.is_file():
            raise BackupError("backup is not restorable")
        checksum = await asyncio.to_thread(_sha256, path)
        if not row.get("sha256") or checksum != row["sha256"]:
            raise BackupError("backup checksum mismatch")
        validation = await asyncio.to_thread(_validate_file, path)
        current = await self.repository.schema_version()
        if validation["schema_version"] != current:
            raise BackupError("backup schema version mismatch")
        return {
            "backup_id": backup_id,
            "valid": True,
            "dry_run": True,
            "schema_version": current,
            "size_bytes": path.stat().st_size,
            "sha256": checksum,
            "next_step": "offline_restore_required",
        }

    def _safe_path(self, value: str) -> Path:
        path = Path(value).resolve()
        try:
            path.relative_to(self.backup_dir)
        except ValueError as error:
            raise BackupError("backup path is outside managed directory") from error
        return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_file(path: Path) -> dict[str, str]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise BackupError("backup integrity check failed")
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if not row:
            raise BackupError("backup schema version missing")
        return {"schema_version": str(row[0])}
    finally:
        connection.close()
