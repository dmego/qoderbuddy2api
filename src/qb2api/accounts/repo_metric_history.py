"""Metric history persistence (credit/points time series)."""

from __future__ import annotations

import json
from typing import Any

from .schema import now_iso


class MetricHistoryRepositoryMixin:
    async def upsert_metric_history(
        self,
        *,
        provider: str,
        account_id: str,
        metric_kind: str,
        value: Any,
        status: str = "fresh",
        observed_at: str | None = None,
        expires_at: str | None = None,
    ) -> None:
        async with self._operation(write=True) as db:
            await db.execute(
                """
                INSERT INTO account_metric_history
                    (provider, account_id, metric_kind, metric_value_json,
                     observed_at, expires_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, account_id, metric_kind, observed_at) DO UPDATE SET
                    metric_value_json=excluded.metric_value_json,
                    expires_at=excluded.expires_at,
                    status=excluded.status
                """,
                (
                    provider,
                    account_id,
                    metric_kind,
                    json.dumps(value, ensure_ascii=False),
                    observed_at or now_iso(),
                    expires_at,
                    status,
                ),
            )

    async def list_metric_history(
        self,
        *,
        provider: str,
        account_id: str,
        metric_kind: str,
        limit: int = 500,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT * FROM account_metric_history
            WHERE provider=? AND account_id=? AND metric_kind=?
        """
        params: list[Any] = [provider, account_id, metric_kind]
        if since:
            query += " AND observed_at >= ?"
            params.append(since)
        query += " ORDER BY observed_at DESC LIMIT ?"
        params.append(limit)
        async with self._operation() as db:
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
        return [self._metric_history_row(row) for row in reversed(rows)]

    async def delete_metric_history_before(self, before_iso: str) -> int:
        async with self._operation(write=True) as db:
            cursor = await db.execute(
                "DELETE FROM account_metric_history WHERE observed_at < ?",
                (before_iso,),
            )
        return cursor.rowcount

    @staticmethod
    def _metric_history_row(row: Any) -> dict[str, Any]:
        result = dict(row)
        try:
            result["value"] = json.loads(result.pop("metric_value_json"))
        except (TypeError, json.JSONDecodeError):
            result["value"] = None
        return result
