"""Secret-safe lifecycle events and trackable admin operations."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from .schema import now_iso

_SERVICE_ERROR_CODES = frozenset({"worker_state_error", "service_operation_failed"})


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
        await super().save_service_operation(operation)
        await self.add_service_event(
            service_name="proxy-worker",
            event_type="operation",
            action=operation.action,
            operation_id=operation.operation_id,
            status=operation.status,
            error_code="service_operation_failed" if operation.error else None,
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
        error_code: str | None = None,
    ) -> int:
        async with self._operation(write=True) as db:
            cursor = await db.execute(
                """
                INSERT INTO service_events (
                    event_id, service_name, event_type, action, desired_state,
                    observed_state, operation_id, status, error_code, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()), service_name, event_type, action, desired_state,
                    observed_state,
                    operation_id,
                    status,
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
    ) -> tuple[list[dict[str, Any]], int | None]:
        where = "WHERE cursor < ?" if cursor is not None else ""
        params: tuple[int, ...] = (cursor, limit + 1) if cursor is not None else (limit + 1,)
        async with self._operation() as db:
            rows = await (
                await db.execute(
                    f"SELECT * FROM service_events {where} ORDER BY cursor DESC LIMIT ?",
                    params,
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
        except Exception as error:
            status = "failed"
            await self.finish_metric_refresh_operation(
                operation_id,
                status=status,
                error_code=type(error).__name__,
            )
        else:
            status = "succeeded"
            await self.finish_metric_refresh_operation(
                operation_id,
                status=status,
                result=result,
            )
        await self._audit_metric_refresh(operation_id, status)

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
        resource_type: str | None = None,
        result: str | None = None,
        started_after: str | None = None,
        started_before: str | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        clauses, params = _audit_filters(
            action, resource_type, result, started_after, started_before
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
    if started_after:
        clauses.append("created_at>=?")
        params.append(started_after)
    if started_before:
        clauses.append("created_at<?")
        params.append(started_before)
    return clauses, params


def _audit_row(row: Any) -> dict[str, Any]:
    result = dict(row)
    try:
        result["metadata"] = json.loads(result.pop("metadata_json"))
    except (TypeError, json.JSONDecodeError):
        result["metadata"] = {}
    return result
