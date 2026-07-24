"""Repository methods for request events, rollups, and account metrics."""

from __future__ import annotations

import json
from typing import Any

from .schema import now_iso
from .telemetry_queries import filtered_usage_rollups, percentile, telemetry_filters


class TelemetryRepositoryMixin:
    async def add_request_event(self, event: dict[str, Any]) -> None:
        fields = (
            "event_id", "request_id", "provider", "account_id", "model_id", "protocol",
            "status", "http_status", "input_tokens", "output_tokens", "latency_ms",
            "stream_committed", "started_at", "finished_at", "error_code", "redacted_error",
        )
        values = [event.get(field) for field in fields]
        values[11] = int(bool(values[11]))
        values[12] = values[12] or now_iso()
        async with self._operation(write=True) as db:
            await db.execute(
                "INSERT OR IGNORE INTO request_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )

    async def add_request_events(self, events: list[dict[str, Any]]) -> int:
        if not events:
            return 0
        fields = (
            "event_id", "request_id", "provider", "account_id", "model_id", "protocol",
            "status", "http_status", "input_tokens", "output_tokens", "latency_ms",
            "stream_committed", "started_at", "finished_at", "error_code", "redacted_error",
        )
        rows = []
        for event in events[:100]:
            values = [event.get(field) for field in fields]
            values[11] = int(bool(values[11]))
            values[12] = values[12] or now_iso()
            rows.append(values)
        async with self._operation(write=True) as db:
            await db.executemany(
                "INSERT OR IGNORE INTO request_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    async def list_request_events(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        provider: str | None = None,
        account_id: str | None = None,
        model_id: str | None = None,
        status: str | None = None,
        started_after: str | None = None,
        started_before: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 501))
        offset = max(0, offset)
        where, params = telemetry_filters(
            "started_at", provider=provider, account_id=account_id, model_id=model_id,
            status=status, started_after=started_after, started_before=started_before,
        )
        async with self._operation() as db:
            cursor = await db.execute(
                f"SELECT * FROM request_events WHERE {where} "
                "ORDER BY started_at DESC, event_id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            rows = await cursor.fetchall()
        return [self._event_row(row) for row in rows]

    async def get_request_event(self, event_id: str) -> dict[str, Any] | None:
        async with self._operation() as db:
            cursor = await db.execute(
                "SELECT * FROM request_events WHERE event_id=?", (event_id,)
            )
            row = await cursor.fetchone()
        return self._event_row(row) if row else None

    async def list_usage_rollups(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        bucket_kind: str | None = None,
        provider: str | None = None,
        account_id: str | None = None,
        model_id: str | None = None,
        status: str | None = None,
        started_after: str | None = None,
        started_before: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 501))
        offset = max(0, offset)
        if status is not None:
            async with self._operation() as db:
                return await filtered_usage_rollups(
                    db, limit=limit, offset=offset, bucket_kind=bucket_kind or "minute",
                    provider=provider, account_id=account_id, model_id=model_id,
                    status=status, started_after=started_after, started_before=started_before,
                )
        where, params = telemetry_filters(
            "bucket_start", provider=provider, account_id=account_id, model_id=model_id,
            status=None, started_after=started_after, started_before=started_before,
        )
        if bucket_kind:
            where += " AND bucket_kind=?"
            params.append(bucket_kind)
        async with self._operation() as db:
            cursor = await db.execute(
                f"SELECT * FROM usage_rollups WHERE {where} "
                "ORDER BY bucket_start DESC, provider, account_id, model_id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def usage_summary(
        self,
        *,
        provider: str | None = None,
        account_id: str | None = None,
        model_id: str | None = None,
        status: str | None = None,
        started_after: str | None = None,
        started_before: str | None = None,
    ) -> dict[str, Any]:
        where, params = telemetry_filters(
            "started_at", provider=provider, account_id=account_id, model_id=model_id,
            status=status, started_after=started_after, started_before=started_before,
        )
        async with self._operation() as db:
            cursor = await db.execute(
                f"""
                SELECT COUNT(*), COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),
                       COALESCE(SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN status!='succeeded' THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN input_tokens IS NOT NULL OR output_tokens IS NOT NULL
                                         THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN input_tokens IS NULL AND output_tokens IS NULL
                                         THEN 1 ELSE 0 END), 0)
                FROM request_events WHERE {where}
                """
                , params
            )
            row = await cursor.fetchone()
            latency_rows = await (await db.execute(
                f"SELECT latency_ms FROM request_events WHERE {where} "
                "AND latency_ms IS NOT NULL ORDER BY latency_ms",
                params,
            )).fetchall()
        latencies = [int(item[0]) for item in latency_rows]
        return {
            "request_count": int(row[0]),
            "input_tokens": int(row[1]),
            "output_tokens": int(row[2]),
            "success_count": int(row[3]),
            "error_count": int(row[4]),
            "token_event_count": int(row[5]),
            "missing_token_count": int(row[6]),
            "avg_latency_ms": (
                sum(latencies) / len(latencies) if latencies else None
            ),
            "p95_latency_ms": percentile(latencies, 0.95),
        }

    async def upsert_usage_rollup(self, values: dict[str, Any]) -> None:
        fields = (
            "bucket_start", "bucket_kind", "provider", "account_id", "model_id", "request_count",
            "success_count", "error_count", "input_tokens", "output_tokens",
            "token_event_count", "missing_token_count", "latency_p50_ms", "latency_p95_ms",
        )
        params = [values.get(field) for field in fields] + [now_iso()]
        async with self._operation(write=True) as db:
            await db.execute(
                """
                INSERT INTO usage_rollups
                    (bucket_start, bucket_kind, provider, account_id, model_id, request_count,
                     success_count, error_count, input_tokens, output_tokens,
                     token_event_count, missing_token_count, latency_p50_ms,
                     latency_p95_ms, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bucket_start, bucket_kind, provider, account_id, model_id) DO UPDATE SET
                    request_count=excluded.request_count, success_count=excluded.success_count,
                    error_count=excluded.error_count, input_tokens=excluded.input_tokens,
                    output_tokens=excluded.output_tokens,
                    token_event_count=excluded.token_event_count,
                    missing_token_count=excluded.missing_token_count,
                    latency_p50_ms=excluded.latency_p50_ms,
                    latency_p95_ms=excluded.latency_p95_ms, updated_at=excluded.updated_at
                """,
                params,
            )

    async def request_events_between(self, started_at: str, ended_at: str) -> list[dict[str, Any]]:
        async with self._operation() as db:
            cursor = await db.execute(
                """
                SELECT provider, account_id, model_id, status, input_tokens,
                       output_tokens, latency_ms
                FROM request_events
                WHERE started_at >= ? AND started_at < ?
                ORDER BY started_at
                """,
                (started_at, ended_at),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def prune_request_events(self, before: str) -> int:
        async with self._operation(write=True) as db:
            cursor = await db.execute(
                "DELETE FROM request_events WHERE started_at < ?",
                (before,),
            )
        return max(0, cursor.rowcount)

    async def upsert_metric_snapshot(self, *, provider: str, account_id: str, metric_kind: str,
                                     value: Any, observed_at: str | None = None, expires_at: str | None = None,
                                     status: str = "fresh", last_error: str | None = None) -> None:
        async with self._operation(write=True) as db:
            await db.execute(
                """
                INSERT INTO account_metric_snapshots
                    (provider, account_id, metric_kind, metric_value_json, observed_at,
                     expires_at, status, last_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, account_id, metric_kind) DO UPDATE SET
                    metric_value_json=excluded.metric_value_json, observed_at=excluded.observed_at,
                    expires_at=excluded.expires_at, status=excluded.status, last_error=excluded.last_error
                """,
                (provider, account_id, metric_kind, json.dumps(value, ensure_ascii=False), observed_at or now_iso(),
                 expires_at, status, last_error),
            )

    async def list_metric_snapshots(
        self,
        provider: str | None = None,
        account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        async with self._operation() as db:
            if provider and account_id:
                cursor = await db.execute(
                    """
                    SELECT * FROM account_metric_snapshots
                    WHERE provider=? AND account_id=? ORDER BY metric_kind
                    """,
                    (provider, account_id),
                )
            elif provider:
                cursor = await db.execute(
                    "SELECT * FROM account_metric_snapshots WHERE provider=? ORDER BY account_id, metric_kind",
                    (provider,),
                )
            else:
                cursor = await db.execute("SELECT * FROM account_metric_snapshots ORDER BY provider, account_id")
            rows = await cursor.fetchall()
        return [self._metric_row(row) for row in rows]

    @staticmethod
    def _event_row(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["stream_committed"] = bool(result.get("stream_committed"))
        return result

    @staticmethod
    def _metric_row(row: Any) -> dict[str, Any]:
        result = dict(row)
        try:
            result["value"] = json.loads(result.pop("metric_value_json"))
        except (TypeError, json.JSONDecodeError):
            result["value"] = None
        return result
