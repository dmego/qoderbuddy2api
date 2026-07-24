"""Usage aggregation and request-event retention service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from qb2api.accounts.repository import AccountRepository
from qb2api.config import Settings


@dataclass(frozen=True)
class RollupWindow:
    kind: str
    start: datetime

    @property
    def end(self) -> datetime:
        if self.kind == "minute":
            return self.start + timedelta(minutes=1)
        if self.kind == "day":
            return self.start + timedelta(days=1)
        month = 1 if self.start.month == 12 else self.start.month + 1
        year = self.start.year + 1 if self.start.month == 12 else self.start.year
        return self.start.replace(year=year, month=month)


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
        for window in _rollup_windows(current):
            events = await self.repository.request_events_between(
                window.start.isoformat(),
                window.end.isoformat(),
            )
            for values in _aggregate(events, window):
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


def _rollup_windows(current: datetime) -> tuple[RollupWindow, ...]:
    return (
        RollupWindow("minute", current.replace(second=0, microsecond=0)),
        RollupWindow("day", current.replace(hour=0, minute=0, second=0, microsecond=0)),
        RollupWindow("month", current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)),
    )


def _aggregate(events: list[dict[str, Any]], window: RollupWindow) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for event in events:
        key = (
            str(event.get("provider") or "unknown"),
            str(event.get("account_id") or ""),
            str(event.get("model_id") or "unknown"),
        )
        groups.setdefault(key, []).append(event)
    return [_group_values(key, rows, window) for key, rows in groups.items()]


def _group_values(
    key: tuple[str, str, str],
    events: list[dict[str, Any]],
    window: RollupWindow,
) -> dict[str, Any]:
    return {
        "bucket_start": window.start.isoformat(),
        "bucket_kind": window.kind,
        "provider": key[0],
        "account_id": key[1],
        "model_id": key[2],
        **_request_counts(events),
        **_token_counts(events),
        **_latency_percentiles(events),
    }


def _request_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "request_count": len(events),
        "success_count": sum(event.get("status") == "succeeded" for event in events),
        "error_count": sum(event.get("status") != "succeeded" for event in events),
    }


def _token_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    token_events = [
        event
        for event in events
        if event.get("input_tokens") is not None or event.get("output_tokens") is not None
    ]
    return {
        "input_tokens": sum(int(event.get("input_tokens") or 0) for event in token_events),
        "output_tokens": sum(int(event.get("output_tokens") or 0) for event in token_events),
        "token_event_count": len(token_events),
        "missing_token_count": len(events) - len(token_events),
    }


def _latency_percentiles(events: list[dict[str, Any]]) -> dict[str, int | None]:
    latencies = sorted(
        int(event["latency_ms"])
        for event in events
        if isinstance(event.get("latency_ms"), int)
    )
    return {
        "latency_p50_ms": _percentile(latencies, 0.50),
        "latency_p95_ms": _percentile(latencies, 0.95),
    }


def _percentile(values: list[int], ratio: float) -> int | None:
    if not values:
        return None
    index = min(len(values) - 1, max(0, round((len(values) - 1) * ratio)))
    return values[index]
