"""Check-in run, attempt, and daily-state persistence methods."""

from __future__ import annotations

from typing import Any

from .schema import now_iso


class CheckinRepositoryMixin:
    async def get_checkin_daily_state(
        self,
        provider: str,
        account_id: str,
        local_date: str,
        timezone: str,
    ) -> dict[str, Any] | None:
        async with self._operation() as db:
            cursor = await db.execute(
                """
                SELECT * FROM checkin_daily_state
                WHERE provider=? AND account_id=? AND local_date=? AND timezone=?
                """,
                (provider, account_id, local_date, timezone),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_checkin_daily_states(
        self, local_date: str, timezone: str
    ) -> list[dict[str, Any]]:
        async with self._operation() as db:
            cursor = await db.execute(
                """
                SELECT * FROM checkin_daily_state
                WHERE local_date=? AND timezone=?
                ORDER BY provider, account_id
                """,
                (local_date, timezone),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def set_checkin_daily_state(
        self,
        *,
        provider: str,
        account_id: str,
        local_date: str,
        timezone: str,
        terminal_outcome: str | None,
        last_run_id: str | None = None,
    ) -> None:
        values = (
            provider,
            account_id,
            local_date,
            timezone,
            terminal_outcome,
            last_run_id,
            now_iso(),
        )
        async with self._operation(write=True) as db:
            await db.execute(_UPSERT_DAILY_STATE, values)

    async def create_checkin_run(
        self,
        *,
        run_id: str,
        local_date: str,
        timezone: str,
        trigger: str = "scheduler",
    ) -> None:
        async with self._operation(write=True) as db:
            await db.execute(
                """
                INSERT INTO checkin_runs (
                    run_id, local_date, timezone, started_at, status, trigger
                ) VALUES (?, ?, ?, ?, 'running', ?)
                """,
                (run_id, local_date, timezone, now_iso(), trigger),
            )

    async def finish_checkin_run(
        self,
        run_id: str,
        *,
        status: str = "finished",
        error_message: str | None = None,
    ) -> None:
        async with self._operation(write=True) as db:
            await db.execute(
                """
                UPDATE checkin_runs
                SET finished_at=?, status=?, error_message=?
                WHERE run_id=?
                """,
                (now_iso(), status, error_message, run_id),
            )

    async def upsert_checkin_attempt(
        self,
        *,
        run_id: str,
        provider: str,
        account_id: str,
        outcome: str | None,
        http_status: int | None = None,
        business_code: str | None = None,
        request_id: str | None = None,
        attempts: int = 1,
        redacted_error: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        now = now_iso()
        values = (
            run_id,
            provider,
            account_id,
            outcome,
            http_status,
            business_code,
            request_id,
            attempts,
            started_at or now,
            finished_at or now,
            redacted_error,
        )
        async with self._operation(write=True) as db:
            await db.execute(_UPSERT_ATTEMPT, values)

    async def list_checkin_attempts(self, run_id: str) -> list[dict[str, Any]]:
        async with self._operation() as db:
            cursor = await db.execute(
                """
                SELECT * FROM checkin_attempts
                WHERE run_id=? ORDER BY provider, account_id
                """,
                (run_id,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_checkin_run(self, run_id: str) -> dict[str, Any] | None:
        async with self._operation() as db:
            cursor = await db.execute(
                "SELECT * FROM checkin_runs WHERE run_id=?",
                (run_id,),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_checkin_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return secret-free run summaries ordered by most recent start."""
        async with self._operation() as db:
            cursor = await db.execute(
                """
                SELECT r.run_id, r.local_date, r.timezone, r.started_at,
                       r.finished_at, r.status, r.trigger,
                       COUNT(a.run_id) AS attempt_count,
                       SUM(CASE WHEN a.outcome IN ('CLAIMED', 'ALREADY_CHECKED_IN', 'SKIPPED')
                           THEN 1 ELSE 0 END) AS successful_count
                FROM checkin_runs AS r
                LEFT JOIN checkin_attempts AS a ON a.run_id = r.run_id
                GROUP BY r.run_id
                ORDER BY r.started_at DESC, r.run_id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]


_UPSERT_DAILY_STATE = """
INSERT INTO checkin_daily_state (
    provider, account_id, local_date, timezone,
    terminal_outcome, last_run_id, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(provider, account_id, local_date, timezone) DO UPDATE SET
    terminal_outcome=excluded.terminal_outcome,
    last_run_id=excluded.last_run_id,
    updated_at=excluded.updated_at
"""

_UPSERT_ATTEMPT = """
INSERT INTO checkin_attempts (
    run_id, provider, account_id, outcome, http_status, business_code,
    request_id, attempts, started_at, finished_at, redacted_error
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(run_id, provider, account_id) DO UPDATE SET
    outcome=excluded.outcome,
    http_status=excluded.http_status,
    business_code=excluded.business_code,
    request_id=excluded.request_id,
    attempts=excluded.attempts,
    finished_at=excluded.finished_at,
    redacted_error=excluded.redacted_error
"""
