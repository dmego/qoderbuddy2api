"""Check-in run, attempt, and daily-state persistence methods."""

from __future__ import annotations

import json
from typing import Any

from .schema import now_iso


class CheckinRepositoryMixin:
    async def get_checkin_daily_state(
        self,
        provider: str,
        account_id: str,
        local_date: str,
        *legacy_timezone: str,
        timezone: str | None = None,
    ) -> dict[str, Any] | None:
        selected_timezone = _daily_state_timezone(legacy_timezone, timezone=timezone)
        async with self._operation() as db:
            cursor = await db.execute(
                """
                SELECT * FROM checkin_daily_state
                WHERE provider=? AND account_id=? AND local_date=? AND timezone=?
                """,
                (provider, account_id, local_date, selected_timezone),
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
        reward_credits: float | None = None,
        reward_expires_at: str | None = None,
        quota_before: dict[str, Any] | None = None,
        quota_after: dict[str, Any] | None = None,
        quota_delta: dict[str, Any] | None = None,
        quota_observed_at: str | None = None,
        quota_change_status: str | None = None,
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
            reward_credits,
            reward_expires_at,
            _json(quota_before),
            _json(quota_after),
            _json(quota_delta),
            quota_observed_at,
            quota_change_status,
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
        return [_attempt_row(row) for row in rows]

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
        rows, _ = await self.list_checkin_runs_page(limit=limit)
        return rows

    async def list_checkin_runs_page(
        self,
        *,
        limit: int = 20,
        cursor: tuple[str, str] | None = None,
        status: str | None = None,
        trigger: str | None = None,
    ) -> tuple[list[dict[str, Any]], tuple[str, str] | None]:
        where, params = _run_filters(cursor, status, trigger)
        async with self._operation() as db:
            cursor = await db.execute(
                f"""
                SELECT r.run_id, r.local_date, r.timezone, r.started_at,
                       r.finished_at, r.status, r.trigger,
                       COUNT(a.run_id) AS attempt_count,
                       SUM(CASE WHEN a.outcome IN ('CLAIMED', 'ALREADY_CHECKED_IN', 'SKIPPED')
                           THEN 1 ELSE 0 END) AS successful_count
                FROM checkin_runs AS r
                LEFT JOIN checkin_attempts AS a ON a.run_id = r.run_id
                WHERE {where}
                GROUP BY r.run_id
                ORDER BY r.started_at DESC, r.run_id DESC
                LIMIT ?
                """,
                (*params, limit + 1),
            )
            rows = await cursor.fetchall()
        page = [dict(row) for row in rows[:limit]]
        next_key = None
        if len(rows) > limit and page:
            next_key = (page[-1]["started_at"], page[-1]["run_id"])
        return page, next_key


def _run_filters(
    cursor: tuple[str, str] | None,
    status: str | None,
    trigger: str | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        clauses.append("r.status=?")
        params.append(status)
    if trigger is not None:
        clauses.append("r.trigger=?")
        params.append(trigger)
    if cursor is not None:
        clauses.append("(r.started_at < ? OR (r.started_at = ? AND r.run_id < ?))")
        params.extend((cursor[0], cursor[0], cursor[1]))
    return " AND ".join(clauses) or "1=1", params


def _daily_state_timezone(legacy: tuple[str, ...], *, timezone: str | None) -> str:
    if timezone is not None:
        if legacy:
            raise TypeError("timezone passed both positionally and by keyword")
        return timezone
    if len(legacy) != 1:
        raise TypeError("get_checkin_daily_state requires a timezone")
    return legacy[0]


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
    request_id, attempts, started_at, finished_at, redacted_error, reward_credits,
    reward_expires_at, quota_before_json, quota_after_json, quota_delta_json, quota_observed_at, quota_change_status
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(run_id, provider, account_id) DO UPDATE SET
    outcome=excluded.outcome,
    http_status=excluded.http_status,
    business_code=excluded.business_code,
    request_id=excluded.request_id,
    attempts=excluded.attempts,
    finished_at=excluded.finished_at,
    redacted_error=excluded.redacted_error,
    reward_credits=excluded.reward_credits,
    reward_expires_at=excluded.reward_expires_at,
    quota_before_json=excluded.quota_before_json,
    quota_after_json=excluded.quota_after_json,
    quota_delta_json=excluded.quota_delta_json,
    quota_observed_at=excluded.quota_observed_at,
    quota_change_status=excluded.quota_change_status
"""


def _json(value: dict[str, Any] | None) -> str | None:
    return json.dumps(value, ensure_ascii=False) if value is not None else None


def _attempt_row(row: Any) -> dict[str, Any]:
    result = dict(row)
    fields = (
        ("quota_before_json", "quota_before"),
        ("quota_after_json", "quota_after"),
        ("quota_delta_json", "quota_delta"),
    )
    for source, target in fields:
        value = result.pop(source, None)
        try:
            result[target] = json.loads(value) if value else None
        except (TypeError, json.JSONDecodeError):
            result[target] = None
    return result
