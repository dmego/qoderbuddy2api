"""Usage aggregation and request-event retention service."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from qb2api.accounts.repository import AccountRepository
from qb2api.config import Settings


class UsageRollupService:
    def __init__(self, *, settings: Settings, repository: AccountRepository) -> None:
        self.settings = settings
        self.repository = repository
        self._task: asyncio.Task[None] | None = None
        self._wakeup = asyncio.Event()
        self._stopped = False

    def start(self) -> None:
        if self._task is None:
            self._stopped = False
            self._task = asyncio.create_task(self._run(), name="qb2api-usage-rollups")

    async def stop(self) -> None:
        self._stopped = True
        self._wakeup.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    def reconfigure(self) -> None:
        self._wakeup.set()

    async def rollup_once(self, now: datetime | None = None) -> dict[str, int]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        groups = 0
        for kind, start in _bucket_starts(current).items():
            events = await self.repository.request_events_between(
                start.isoformat(),
                _bucket_end(kind, start).isoformat(),
            )
            for values in _aggregate(events, kind, start):
                await self.repository.upsert_usage_rollup(values)
                groups += 1
        cutoff = current - timedelta(days=max(1, self.settings.usage_detail_retention_days))
        deleted = await self.repository.prune_request_events(cutoff.isoformat())
        return {"groups": groups, "deleted_events": deleted}

    async def _run(self) -> None:
        while not self._stopped:
            await self.rollup_once()
            self._wakeup.clear()
            try:
                await asyncio.wait_for(
                    self._wakeup.wait(),
                    timeout=max(30, self.settings.usage_rollup_interval_seconds),
                )
            except TimeoutError:
                pass


def _bucket_starts(current: datetime) -> dict[str, datetime]:
    return {
        "minute": current.replace(second=0, microsecond=0),
        "day": current.replace(hour=0, minute=0, second=0, microsecond=0),
        "month": current.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
    }


def _bucket_end(kind: str, start: datetime) -> datetime:
    if kind == "minute":
        return start + timedelta(minutes=1)
    if kind == "day":
        return start + timedelta(days=1)
    next_month = 1 if start.month == 12 else start.month + 1
    next_year = start.year + 1 if start.month == 12 else start.year
    return start.replace(year=next_year, month=next_month)


def _aggregate(events: list[dict[str, Any]], kind: str, start: datetime) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for event in events:
        key = (
            str(event.get("provider") or "unknown"),
            str(event.get("account_id") or ""),
            str(event.get("model_id") or "unknown"),
        )
        groups.setdefault(key, []).append(event)
    return [_group_values(key, rows, kind, start) for key, rows in groups.items()]


def _group_values(key, events, kind: str, start: datetime) -> dict[str, Any]:
    latencies = sorted(
        int(event["latency_ms"])
        for event in events
        if isinstance(event.get("latency_ms"), int)
    )
    token_events = [
        event for event in events
        if event.get("input_tokens") is not None or event.get("output_tokens") is not None
    ]
    return {
        "bucket_start": start.isoformat(),
        "bucket_kind": kind,
        "provider": key[0],
        "account_id": key[1],
        "model_id": key[2],
        "request_count": len(events),
        "success_count": sum(event.get("status") == "succeeded" for event in events),
        "error_count": sum(event.get("status") != "succeeded" for event in events),
        "input_tokens": sum(int(event.get("input_tokens") or 0) for event in token_events),
        "output_tokens": sum(int(event.get("output_tokens") or 0) for event in token_events),
        "token_event_count": len(token_events),
        "missing_token_count": len(events) - len(token_events),
        "latency_p50_ms": _percentile(latencies, 0.50),
        "latency_p95_ms": _percentile(latencies, 0.95),
    }


def _percentile(values: list[int], ratio: float) -> int | None:
    if not values:
        return None
    index = min(len(values) - 1, max(0, round((len(values) - 1) * ratio)))
    return values[index]
