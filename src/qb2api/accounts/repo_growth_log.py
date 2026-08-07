"""Growth automation log persistence methods."""

from __future__ import annotations

import json
from typing import Any


class GrowthLogMixin:
    async def insert_growth_log(
        self,
        *,
        provider: str,
        account_id: str,
        triggered_by: str,
        results: dict[str, Any],
    ) -> int:
        async with self._operation(write=True) as db:
            cursor = await db.execute(
                """
                INSERT INTO growth_automation_log
                    (provider, account_id, triggered_by, results_json)
                VALUES (?, ?, ?, ?)
                """,
                (provider, account_id, triggered_by, json.dumps(results, ensure_ascii=False)),
            )
            return cursor.lastrowid or 0

    async def list_growth_logs(
        self,
        *,
        provider: str,
        account_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        async with self._operation() as db:
            cursor = await db.execute(
                """
                SELECT id, provider, account_id, triggered_by,
                       results_json, created_at
                FROM growth_automation_log
                WHERE provider=? AND account_id=?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (provider, account_id, limit, offset),
            )
            rows = await cursor.fetchall()
        return [
            {
                "id": row["id"],
                "provider": row["provider"],
                "account_id": row["account_id"],
                "triggered_by": row["triggered_by"],
                "results": json.loads(row["results_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def count_growth_logs(self, *, provider: str, account_id: str) -> int:
        async with self._operation() as db:
            cursor = await db.execute(
                """
                SELECT COUNT(*) AS count FROM growth_automation_log
                WHERE provider=? AND account_id=?
                """,
                (provider, account_id),
            )
            row = await cursor.fetchone()
        return int(row["count"]) if row else 0
