"""Trackable metric-refresh operation persistence."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from .schema import now_iso

_LOGGER = logging.getLogger(__name__)


class MetricRefreshRepositoryMixin:
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
        return _metric_refresh_row(row)

    async def run_metric_refresh_operation(self, operation_id: str, scheduler: Any) -> None:
        try:
            result = await scheduler.refresh_once()
        except asyncio.CancelledError:
            await self._finish_metric_refresh(
                operation_id,
                status="cancelled",
                error_code="refresh_cancelled",
            )
            raise
        except Exception:
            _LOGGER.error(
                "metrics refresh operation failed",
                extra={"operation_id": operation_id, "error_code": "metrics_refresh_failed"},
            )
            await self._finish_metric_refresh(
                operation_id,
                status="failed",
                error_code="metrics_refresh_failed",
            )
        else:
            await self._finish_metric_refresh(operation_id, status="succeeded", result=result)

    async def _finish_metric_refresh(
        self,
        operation_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None:
        async with self.transaction():
            await self.finish_metric_refresh_operation(
                operation_id,
                status=status,
                result=result,
                error_code=error_code,
            )
            await self._audit_metric_refresh(operation_id, status, error_code)

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

    async def _audit_metric_refresh(
        self,
        operation_id: str,
        result: str,
        error_code: str | None = None,
    ) -> None:
        await self.add_audit_event(
            actor_type="admin",
            actor_id=None,
            action="metrics.refresh",
            resource_type="metrics",
            resource_id=operation_id,
            result=result,
            metadata={"error_code": error_code} if error_code else None,
        )


def _metric_refresh_row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    try:
        result["result"] = json.loads(result.pop("result_json"))
    except (TypeError, json.JSONDecodeError):
        result["result"] = {}
    return result
