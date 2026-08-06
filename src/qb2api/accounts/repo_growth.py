"""Durable WorkBuddy active-day reservations."""

from __future__ import annotations

from typing import Any

from .schema import now_iso


class GrowthRepositoryMixin:
    async def claim_workbuddy_active_day(
        self,
        *,
        provider: str,
        account_id: str,
        local_date: str,
        timezone: str,
    ) -> bool:
        now = now_iso()
        async with self._operation(write=True) as db:
            cursor = await db.execute(
                """
                INSERT INTO workbuddy_active_days (
                    provider, account_id, local_date, timezone,
                    status, started_at, updated_at
                ) VALUES (?, ?, ?, ?, 'running', ?, ?)
                ON CONFLICT(provider, account_id, local_date, timezone) DO NOTHING
                """,
                (provider, account_id, local_date, timezone, now, now),
            )
            return cursor.rowcount == 1

    async def finish_workbuddy_active_day(
        self,
        *,
        provider: str,
        account_id: str,
        local_date: str,
        timezone: str,
        status: str,
        error_code: str | None = None,
    ) -> None:
        if status not in {"succeeded", "failed"}:
            raise ValueError("invalid active-day status")
        now = now_iso()
        async with self._operation(write=True) as db:
            await db.execute(
                """
                UPDATE workbuddy_active_days
                SET status=?, error_code=?, finished_at=?, updated_at=?
                WHERE provider=? AND account_id=? AND local_date=? AND timezone=?
                """,
                (status, error_code, now, now, provider, account_id, local_date, timezone),
            )

    async def get_workbuddy_active_day(
        self, provider: str, account_id: str, local_date: str, *, timezone: str
    ) -> dict[str, Any] | None:
        async with self._operation() as db:
            cursor = await db.execute(
                "SELECT * FROM workbuddy_active_days WHERE provider=? AND account_id=? AND local_date=? AND timezone=?",
                (provider, account_id, local_date, timezone),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None
