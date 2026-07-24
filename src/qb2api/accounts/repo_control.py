"""Repository methods for runtime control, audit, and backup metadata."""

from __future__ import annotations

import json
from typing import Any

from .schema import now_iso


class ControlRepositoryMixin:
    async def get_runtime_setting(self, key: str) -> dict[str, Any] | None:
        async with self._operation() as db:
            cursor = await db.execute("SELECT * FROM runtime_settings WHERE key=?", (key,))
            row = await cursor.fetchone()
        return self._setting_row(row) if row else None

    async def list_runtime_settings(self) -> list[dict[str, Any]]:
        async with self._operation() as db:
            cursor = await db.execute("SELECT * FROM runtime_settings ORDER BY key")
            rows = await cursor.fetchall()
        return [self._setting_row(row) for row in rows]

    async def update_runtime_setting_status(
        self, key: str, *, status: str, last_error: str | None = None
    ) -> None:
        async with self._operation(write=True) as db:
            await db.execute(
                "UPDATE runtime_settings SET apply_status=?, last_error=?, updated_at=? WHERE key=?",
                (status, last_error, now_iso(), key),
            )

    async def upsert_runtime_setting(
        self,
        *,
        key: str,
        value: Any,
        expected_version: int | None = None,
        source: str = "runtime",
        apply_mode: str = "immediate",
        apply_status: str = "pending",
        last_error: str | None = None,
        updated_by: str | None = None,
    ) -> int:
        async with self._operation(write=True) as db:
            cursor = await db.execute("SELECT value_version FROM runtime_settings WHERE key=?", (key,))
            row = await cursor.fetchone()
            current_version = int(row[0]) if row else 0
            if expected_version is not None and expected_version != current_version:
                raise ValueError(
                    f"runtime setting version conflict: expected {expected_version}, got {current_version}"
                )
            next_version = current_version + 1
            await db.execute(
                """
                INSERT INTO runtime_settings
                    (key, value_json, value_version, source, apply_mode, apply_status,
                     last_error, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json,
                    value_version=excluded.value_version,
                    source=excluded.source,
                    apply_mode=excluded.apply_mode,
                    apply_status=excluded.apply_status,
                    last_error=excluded.last_error,
                    updated_at=excluded.updated_at,
                    updated_by=excluded.updated_by
                """,
                (key, json.dumps(value, ensure_ascii=False), next_version, source, apply_mode, apply_status,
                 last_error, now_iso(), updated_by),
            )
        return next_version

    async def save_service_runtime(self, service_name: str, values: dict[str, Any]) -> None:
        columns = (
            "desired_state", "observed_state", "worker_pid", "process_start_time",
            "process_group_id", "owner_instance_id", "internal_auth_version", "started_at",
            "stopped_at", "last_health_at", "last_exit_code", "last_error",
        )
        params = [values.get(column) for column in columns]
        params.append(now_iso())
        async with self._operation(write=True) as db:
            await db.execute(
                """
                INSERT INTO service_runtime
                    (desired_state, observed_state, worker_pid, process_start_time,
                     process_group_id, owner_instance_id, internal_auth_version, started_at,
                     stopped_at, last_health_at, last_exit_code, last_error, updated_at, service_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(service_name) DO UPDATE SET
                    desired_state=excluded.desired_state, observed_state=excluded.observed_state,
                    worker_pid=excluded.worker_pid, process_start_time=excluded.process_start_time,
                    process_group_id=excluded.process_group_id, owner_instance_id=excluded.owner_instance_id,
                    internal_auth_version=excluded.internal_auth_version, started_at=excluded.started_at,
                    stopped_at=excluded.stopped_at, last_health_at=excluded.last_health_at,
                    last_exit_code=excluded.last_exit_code, last_error=excluded.last_error,
                    updated_at=excluded.updated_at
                """,
                (*params, service_name),
            )

    async def get_service_runtime(self, service_name: str) -> dict[str, Any] | None:
        async with self._operation() as db:
            cursor = await db.execute("SELECT * FROM service_runtime WHERE service_name=?", (service_name,))
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def save_service_operation(self, operation: Any) -> None:
        async with self._operation(write=True) as db:
            await db.execute(
                """
                INSERT INTO service_operations
                    (operation_id, service_name, action, status, error, created_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(operation_id) DO UPDATE SET
                    status=excluded.status, error=excluded.error, finished_at=excluded.finished_at
                """,
                (
                    operation.operation_id, "proxy-worker", operation.action, operation.status,
                    operation.error, str(operation.created_at),
                    str(operation.finished_at) if operation.finished_at else None,
                ),
            )

    async def get_service_operation(self, operation_id: str) -> dict[str, Any] | None:
        async with self._operation() as db:
            cursor = await db.execute("SELECT * FROM service_operations WHERE operation_id=?", (operation_id,))
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_backup_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        async with self._operation() as db:
            cursor = await db.execute("SELECT * FROM backup_runs ORDER BY started_at DESC LIMIT ?", (limit,))
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_backup_run(self, backup_id: str) -> dict[str, Any] | None:
        async with self._operation() as db:
            cursor = await db.execute(
                "SELECT * FROM backup_runs WHERE backup_id=?",
                (backup_id,),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def create_backup_run(
        self,
        *,
        backup_id: str,
        path: str,
        schema_version: str,
    ) -> None:
        async with self._operation(write=True) as db:
            await db.execute(
                """
                INSERT INTO backup_runs
                    (backup_id, path, schema_version, started_at, status)
                VALUES (?, ?, ?, ?, 'running')
                """,
                (backup_id, path, schema_version, now_iso()),
            )

    async def finish_backup_run(
        self,
        backup_id: str,
        *,
        status: str,
        size_bytes: int | None = None,
        sha256: str | None = None,
        error_message: str | None = None,
    ) -> None:
        async with self._operation(write=True) as db:
            await db.execute(
                """
                UPDATE backup_runs
                SET finished_at=?, status=?, size_bytes=?, sha256=?, error_message=?
                WHERE backup_id=?
                """,
                (
                    now_iso(),
                    status,
                    size_bytes,
                    sha256,
                    error_message,
                    backup_id,
                ),
            )

    async def schema_version(self) -> str:
        async with self._operation() as db:
            cursor = await db.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            )
            row = await cursor.fetchone()
        return str(row[0]) if row else "unknown"

    async def add_audit_event(self, *, actor_type: str, actor_id: str | None, action: str,
                              resource_type: str, resource_id: str | None, result: str,
                              metadata: dict[str, Any] | None = None, event_id: str | None = None) -> str:
        import uuid

        event_id = event_id or str(uuid.uuid4())
        async with self._operation(write=True) as db:
            await db.execute(
                "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, actor_type, actor_id, action, resource_type, resource_id,
                 result, json.dumps(metadata or {}, ensure_ascii=False), now_iso()),
            )
        return event_id

    async def list_audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        async with self._operation() as db:
            cursor = await db.execute("SELECT * FROM audit_events ORDER BY created_at DESC LIMIT ?", (limit,))
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def list_proxy_api_keys(self) -> list[dict[str, Any]]:
        async with self._operation() as db:
            cursor = await db.execute(
                """
                SELECT key_id, name, scopes_json, enabled, created_at,
                       last_used_at, expires_at, revoked_at
                FROM proxy_api_keys ORDER BY created_at DESC
                """
            )
            rows = await cursor.fetchall()
        return [_proxy_key_row(row) for row in rows]

    async def list_active_proxy_key_hashes(self) -> list[dict[str, str]]:
        async with self._operation() as db:
            cursor = await db.execute(
                """
                SELECT key_id, key_hash FROM proxy_api_keys
                WHERE enabled=1 AND revoked_at IS NULL
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY created_at
                """,
                (now_iso(),),
            )
            rows = await cursor.fetchall()
        return [{"key_id": str(row[0]), "key_hash": str(row[1])} for row in rows]

    async def create_proxy_api_key(
        self,
        *,
        key_id: str,
        name: str,
        key_hash: str,
        expires_at: str | None,
    ) -> None:
        async with self._operation(write=True) as db:
            await db.execute(
                """
                INSERT INTO proxy_api_keys
                    (key_id, name, key_hash, scopes_json, enabled, created_at, expires_at)
                VALUES (?, ?, ?, '["proxy"]', 1, ?, ?)
                """,
                (key_id, name, key_hash, now_iso(), expires_at),
            )

    async def revoke_proxy_api_key(self, key_id: str) -> bool:
        async with self._operation(write=True) as db:
            cursor = await db.execute(
                """
                UPDATE proxy_api_keys
                SET enabled=0, revoked_at=?
                WHERE key_id=? AND enabled=1 AND revoked_at IS NULL
                """,
                (now_iso(), key_id),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _setting_row(row: Any) -> dict[str, Any]:
        result = dict(row)
        try:
            result["value"] = json.loads(result.pop("value_json"))
        except (TypeError, json.JSONDecodeError):
            result["value"] = None
        return result


def _proxy_key_row(row: Any) -> dict[str, Any]:
    result = dict(row)
    result["enabled"] = bool(result.get("enabled"))
    try:
        result["scopes"] = json.loads(result.pop("scopes_json"))
    except (TypeError, json.JSONDecodeError):
        result["scopes"] = []
        result.pop("scopes_json", None)
    return result
