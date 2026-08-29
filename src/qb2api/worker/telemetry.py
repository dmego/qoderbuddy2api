"""Bounded Worker-to-Control request telemetry transport."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

EventSender = Callable[[list[dict[str, Any]]], Awaitable[None]]


class WorkerTelemetry:
    """Drop-safe queue; telemetry failures never affect proxy responses."""

    _FIELDS = frozenset(
        {
            "event_id", "request_id", "provider", "account_id", "model_id",
            "protocol", "status", "http_status", "input_tokens", "output_tokens",
            "latency_ms", "stream_committed", "started_at", "finished_at",
            "error_code", "redacted_error", "reasoning_effort",
        }
    )

    def __init__(
        self,
        *,
        endpoint: str,
        token: str | None,
        queue_size: int = 1000,
        sender: EventSender | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/") + "/api/control/telemetry"
        self.token = token
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max(10, queue_size))
        self._sender = sender or self._http_sender
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._dropped = 0

    @property
    def dropped(self) -> int:
        return self._dropped

    def start(self) -> None:
        if self._task is None:
            self._stopped = False
            self._task = asyncio.create_task(self._run(), name="qb2api-worker-telemetry")

    def emit(self, event: dict[str, Any]) -> None:
        safe = {key: value for key, value in event.items() if key in self._FIELDS}
        safe.setdefault("started_at", _now())
        try:
            self._queue.put_nowait(safe)
        except asyncio.QueueFull:
            self._dropped += 1

    async def stop(self) -> None:
        self._stopped = True
        task = self._task
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _run(self) -> None:
        while not self._stopped or not self._queue.empty():
            try:
                first = await asyncio.wait_for(self._queue.get(), timeout=0.2)
            except TimeoutError:
                continue
            batch = [first]
            while len(batch) < 50:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await self._sender(batch)
            except Exception:
                self._dropped += len(batch)

    async def _http_sender(self, events: list[dict[str, Any]]) -> None:
        if not self.token:
            self._dropped += len(events)
            return
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=1.0))
        response = await self._client.post(
            self.endpoint,
            headers={"X-QB2API-Worker-Token": self.token},
            json={"events": events},
        )
        response.raise_for_status()


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
