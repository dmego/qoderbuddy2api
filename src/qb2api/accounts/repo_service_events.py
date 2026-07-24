"""Secret-safe lifecycle events and trackable admin operations."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from .schema import now_iso

_SERVICE_ERROR_CODES = frozenset({"worker_state_error", "service_operation_failed"})
_LOGGER = logging.getLogger(__name__)


class ServiceEventRepositoryMixin:
    async def save_service_runtime(self, service_name: str, values: dict[str, Any]) -> None:
        await super().save_service_runtime(service_name, values)
        await self.add_service_event(
            service_name=service_name,
            event_type="state",
            desired_state=values.get("desired_state"),
            observed_state=values.get("observed_state"),
            error_code="worker_state_error" if values.get("last_error") else None,
        )

    async def save_service_operation(self, operation: Any) -> None:
        async with self.transaction():
            await super().save_service_operation(operation)
            await self.add_service_event(
                service_name="proxy-worker",
                event_type="operation",
                action=operation.action,
                operation_id=operation.operation_id,
                status=operation.status,
                in_flight=operation.in_flight,
                error_code="service_operation_failed" if operation.error else None,
            )
            await self.add_audit_event(
                actor_type="admin", actor_id=None,
                action=f"service.{operation.action}", resource_type="service",
                resource_id="proxy-worker", result=operation.status,
                metadata={"operation_id": operation.operation_id},
            )

    async def add_service_event(
        self,
        *,
        service_name: str,
        event_type: str,
        action: str | None = None,
        desired_state: str | None = None,
        observed_state: str | None = None,
        operation_id: str | None = None,
        status: str | None = None,
        in_flight: int | None = None,
        error_code: str | None = None,
    ) -> int:
        async with self._operation(write=True) as db:
            cursor = await db.execute(
                """
                INSERT INTO service_events (
                    event_id, service_name, event_type, action, desired_state,
                    observed_state, operation_id, status, in_flight, error_code, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()), service_name, event_type, action, desired_state,
                    observed_state,
                    operation_id,
                    status,
                    in_flight,
                    error_code if error_code in _SERVICE_ERROR_CODES else None,
                    now_iso(),
                ),
            )
        return int(cursor.lastrowid or 0)

    async def list_service_events(
        self,
        *,
        cursor: int | None = None,
        limit: int = 50,
        event_type: str | None = None,
        status: str | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        clauses: list[str] = []
        params: list[Any] = []
        if cursor is not None:
            clauses.append("cursor < ?")
            params.append(cursor)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit + 1)
        async with self._operation() as db:
            rows = await (
                await db.execute(
                    f"SELECT * FROM service_events {where} ORDER BY cursor DESC LIMIT ?",
                    tuple(params),
                )
            ).fetchall()
        events = [dict(row) for row in rows[:limit]]
        next_cursor = events[-1]["cursor"] if len(rows) > limit and events else None
        return events, next_cursor

    async def create_metric_refresh_operation(self) -> str:
        operation_id = str(uuid.uuid4())
        async with self._operation(write=True) as db:
            await db.execute(
                """
                INSERT INTO metric_refresh_operations
                    (operation_id, status, result_json, error_code, created_at)
                VALUES (?, 'running', '{}', NULL, ?)
                """,
                (operation_id, now_iso()),
            )
        return operation_id

    async def finish_metric_refresh_operation(
        self,
        operation_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None:
        async with self._operation(write=True) as db:
            await db.execute(
                """
                UPDATE metric_refresh_operations
                SET status=?, result_json=?, error_code=?, finished_at=?
                WHERE operation_id=?
                """,
                (status, json.dumps(result or {}), error_code, now_iso(), operation_id),
            )

    async def get_metric_refresh_operation(self, operation_id: str) -> dict[str, Any] | None:
        async with self._operation() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM metric_refresh_operations WHERE operation_id=?",
                    (operation_id,),
                )
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["result"] = json.loads(result.pop("result_json"))
        except (TypeError, json.JSONDecodeError):
            result["result"] = {}
        return result

    async def run_metric_refresh_operation(self, operation_id: str, scheduler: Any) -> None:
        try:
            result = await scheduler.refresh_once()
        except asyncio.CancelledError:
            await self.finish_metric_refresh_operation(
                operation_id,
                status="cancelled",
                error_code="refresh_cancelled",
            )
            await self._audit_metric_refresh(operation_id, "cancelled")
            raise
        except Exception:
            status = "failed"
            _LOGGER.exception("metrics refresh operation failed", extra={"operation_id": operation_id})
            await self.finish_metric_refresh_operation(
                operation_id,
                status=status,
                error_code="metrics_refresh_failed",
            )
        else:
            status = "succeeded"
            await self.finish_metric_refresh_operation(
                operation_id,
                status=status,
                result=result,
            )
        await self._audit_metric_refresh(operation_id, status)

    async def recover_metric_refresh_operations(self) -> list[str]:
        async with self.transaction():
            rows = await (await self.db.execute(
                "SELECT operation_id FROM metric_refresh_operations WHERE status='running'"
            )).fetchall()
            operation_ids = [str(row[0]) for row in rows]
            if operation_ids:
                await self.db.execute(
                    "UPDATE metric_refresh_operations SET status='cancelled', "
                    "error_code='refresh_interrupted', finished_at=? WHERE status='running'",
                    (now_iso(),),
                )
                for operation_id in operation_ids:
                    await self._audit_metric_refresh(operation_id, "cancelled")
        return operation_ids

    async def _audit_metric_refresh(self, operation_id: str, result: str) -> None:
        await self.add_audit_event(
            actor_type="admin",
            actor_id=None,
            action="metrics.refresh",
            resource_type="metrics",
            resource_id=operation_id,
            result=result,
        )

    async def list_audit_events_page(
        self,
        *,
        limit: int,
        offset: int = 0,
        action: str | None = None,
        action_prefix: str | None = None,
        search: str | None = None,
        resource_type: str | None = None,
        result: str | None = None,
        started_after: str | None = None,
        started_before: str | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        clauses, params = _audit_filters(
            action, action_prefix, search, resource_type, result, started_after, started_before
        )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend((limit + 1, offset))
        async with self._operation() as db:
            rows = await (
                await db.execute(
                    f"SELECT * FROM audit_events {where} "
                    "ORDER BY created_at DESC, event_id DESC LIMIT ? OFFSET ?",
                    params,
                )
            ).fetchall()
        events = [_audit_row(row) for row in rows[:limit]]
        next_cursor = offset + limit if len(rows) > limit else None
        return events, next_cursor


def _audit_filters(
    action: str | None,
    action_prefix: str | None,
    search: str | None,
    resource_type: str | None,
    result: str | None,
    started_after: str | None,
    started_before: str | None,
) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (("action", action), ("resource_type", resource_type), ("result", result)):
        if value:
            clauses.append(f"{column}=?")
            params.append(value)
    if action_prefix:
        clauses.append("action LIKE ? ESCAPE '\\'")
        params.append(f"{_escape_like(action_prefix)}.%")
    if search:
        pattern = f"%{_escape_like(search)}%"
        clauses.append(
            "(event_id LIKE ? ESCAPE '\\' OR action LIKE ? ESCAPE '\\' "
            "OR resource_id LIKE ? ESCAPE '\\')"
        )
        params.extend((pattern, pattern, pattern))
    if started_after:
        clauses.append("created_at>=?")
        params.append(started_after)
    if started_before:
        clauses.append("created_at<?")
        params.append(started_before)
    return clauses, params


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _audit_row(row: Any) -> dict[str, Any]:
    result = dict(row)
    try:
        result["metadata"] = json.loads(result.pop("metadata_json"))
    except (TypeError, json.JSONDecodeError):
        result["metadata"] = {}
    return result
