"""Secret-safe lifecycle events and trackable admin operations."""

from __future__ import annotations

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
            in_flight=values.get("in_flight"),
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
            action,
            action_prefix,
            search,
            resource_type=resource_type,
            result=result,
            started_after=started_after,
            started_before=started_before,
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
    *,
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
    error_code = result["metadata"].get("error_code")
    if _safe_error_code(error_code):
        result["error_code"] = error_code
    return result


def _safe_error_code(value: Any) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return False
    return all(char.islower() or char.isdigit() or char in "._-" for char in value)
