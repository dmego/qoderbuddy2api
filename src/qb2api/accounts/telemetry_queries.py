"""SQL helpers for filtered telemetry reads."""

from __future__ import annotations

import math
from typing import Any


def telemetry_filters(
    time_column: str,
    *,
    provider: str | None,
    account_id: str | None,
    model_id: str | None,
    status: str | None,
    started_after: str | None,
    started_before: str | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("provider", provider),
        ("account_id", account_id),
        ("model_id", model_id),
        ("status", status),
    ):
        if value:
            clauses.append(f"{column}=?")
            params.append(value)
    if started_after:
        clauses.append(f"{time_column}>=?")
        params.append(started_after)
    if started_before:
        clauses.append(f"{time_column}<?")
        params.append(started_before)
    return " AND ".join(clauses) or "1=1", params


def percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def raw_rollup_bucket(bucket_kind: str) -> str:
    formats = {
        "minute": "%Y-%m-%dT%H:%M:00+00:00",
        "day": "%Y-%m-%dT00:00:00+00:00",
        "month": "%Y-%m-01T00:00:00+00:00",
    }
    return f"strftime('{formats[bucket_kind]}', started_at)"


async def filtered_usage_rollups(
    db: Any,
    *,
    limit: int,
    offset: int,
    bucket_kind: str,
    provider: str | None,
    account_id: str | None,
    model_id: str | None,
    status: str,
    started_after: str | None,
    started_before: str | None,
) -> list[dict[str, Any]]:
    where, params = telemetry_filters(
        "started_at", provider=provider, account_id=account_id, model_id=model_id,
        status=status, started_after=started_after, started_before=started_before,
    )
    bucket = raw_rollup_bucket(bucket_kind)
    rows = await (await db.execute(
        f"""SELECT {bucket} AS bucket_start, ? AS bucket_kind, provider,
               account_id, model_id, COUNT(*) AS request_count,
               SUM(status='succeeded') AS success_count,
               SUM(status!='succeeded') AS error_count,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               SUM(input_tokens IS NOT NULL OR output_tokens IS NOT NULL) AS token_event_count,
               SUM(input_tokens IS NULL AND output_tokens IS NULL) AS missing_token_count,
               NULL AS latency_p50_ms, NULL AS latency_p95_ms, NULL AS updated_at
        FROM request_events WHERE {where}
        GROUP BY bucket_start, provider, account_id, model_id
        ORDER BY bucket_start DESC, provider, account_id, model_id LIMIT ? OFFSET ?""",
        (bucket_kind, *params, limit, offset),
    )).fetchall()
    return [dict(row) for row in rows]
