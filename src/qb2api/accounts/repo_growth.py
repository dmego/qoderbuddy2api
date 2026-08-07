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
        confirmed: str | None = None,
    ) -> None:
        if status not in {"succeeded", "failed", "skipped_external"}:
            raise ValueError("invalid active-day status")
        now = now_iso()
        async with self._operation(write=True) as db:
            if confirmed is None:
                await db.execute(
                    """
                    UPDATE workbuddy_active_days
                    SET status=?, error_code=?, finished_at=?, updated_at=?
                    WHERE provider=? AND account_id=? AND local_date=? AND timezone=?
                    """,
                    (status, error_code, now, now, provider, account_id, local_date, timezone),
                )
            else:
                await db.execute(
                    """
                    UPDATE workbuddy_active_days
                    SET status=?, error_code=?, finished_at=?,
                        confirmed=?, confirmed_at=?, updated_at=?
                    WHERE provider=? AND account_id=? AND local_date=? AND timezone=?
                    """,
                    (status, error_code, now, confirmed, now, now,
                     provider, account_id, local_date, timezone),
                )

    async def touch_workbuddy_active_day_confirmation(
        self,
        *,
        provider: str,
        account_id: str,
        local_date: str,
        timezone: str,
        confirmed: str | None = None,
    ) -> None:
        """记录一次上游确认结果。confirmed 为 lit/not_lit 时落库，否则仅累加尝试次数。"""
        now = now_iso()
        async with self._operation(write=True) as db:
            if confirmed is None:
                await db.execute(
                    """
                    UPDATE workbuddy_active_days
                    SET confirm_attempts=confirm_attempts+1, updated_at=?
                    WHERE provider=? AND account_id=? AND local_date=? AND timezone=?
                    """,
                    (now, provider, account_id, local_date, timezone),
                )
            else:
                await db.execute(
                    """
                    UPDATE workbuddy_active_days
                    SET confirmed=?, confirmed_at=?, confirm_attempts=confirm_attempts+1, updated_at=?
                    WHERE provider=? AND account_id=? AND local_date=? AND timezone=?
                    """,
                    (confirmed, now, now, provider, account_id, local_date, timezone),
                )

    async def replace_workbuddy_active_day_result(
        self,
        *,
        provider: str,
        account_id: str,
        local_date: str,
        timezone: str,
        status: str,
        error_code: str | None = None,
    ) -> None:
        """手动强制重跑后落库：重置状态并发起确认，不依赖当天唯一锁。"""
        now = now_iso()
        async with self._operation(write=True) as db:
            await db.execute(
                """
                INSERT INTO workbuddy_active_days (
                    provider, account_id, local_date, timezone,
                    status, error_code, started_at, finished_at, updated_at,
                    confirmed, confirmed_at, confirm_attempts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0)
                ON CONFLICT(provider, account_id, local_date, timezone) DO UPDATE SET
                    status=excluded.status,
                    error_code=excluded.error_code,
                    finished_at=excluded.finished_at,
                    updated_at=excluded.updated_at,
                    confirmed=NULL,
                    confirmed_at=NULL,
                    confirm_attempts=0
                """,
                (provider, account_id, local_date, timezone, status, error_code,
                 now, now, now),
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
