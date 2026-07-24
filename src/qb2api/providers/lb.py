"""Dynamic 0..N provider pool with stable-key RR, lease drain, stream commit.

ponytail: no health-check loop / circuit-breaker; 30s key cooldown is enough for MVP.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

from ..openai import ChatCompletionRequest
from .base import Provider

logger = logging.getLogger("qb2api")

_COOLDOWN_S = 30.0


class ProviderUnavailableError(Exception):
    """No active slots available for this provider family."""


@dataclass
class SlotHandle:
    key: str
    provider: Provider
    state: Literal["active", "retiring"] = "active"
    in_flight: int = 0
    generation: int = 0


class DynamicProviderPool(Provider):
    """Stable 0..N pool: RR by slot key, failover pre-commit, lease drain on retire."""

    def __init__(self, name: str):
        self.name = name
        self._lock = asyncio.Lock()
        self._slots: dict[str, SlotHandle] = {}
        self._order: tuple[str, ...] = ()
        self._rr = 0
        self._failed: dict[str, float] = {}  # key -> cooldown_until monotonic
        self._gen = 0

    @property
    def has_available_slots(self) -> bool:
        return bool(self._slots)

    @property
    def instance_count(self) -> int:
        return len(self._slots)

    def _apply_slots_locked(self, slots: dict[str, Provider]) -> list[Provider]:
        """Replace active snapshot. Returns providers safe to close now."""
        to_close: list[Provider] = []
        new_slots: dict[str, SlotHandle] = {}

        for key, provider in slots.items():
            old = self._slots.get(key)
            if old is not None and old.provider is provider:
                old.state = "active"
                new_slots[key] = old
                continue
            if old is not None:
                old.state = "retiring"
                if old.in_flight == 0:
                    to_close.append(old.provider)
            self._gen += 1
            new_slots[key] = SlotHandle(
                key=key, provider=provider, state="active", generation=self._gen
            )

        for key, old in self._slots.items():
            if key in new_slots and new_slots[key] is old:
                continue
            if old.state != "retiring":
                old.state = "retiring"
            if old.in_flight == 0 and old.provider not in to_close:
                # only close if not reused under another key (identity)
                if all(h.provider is not old.provider for h in new_slots.values()):
                    to_close.append(old.provider)

        self._slots = new_slots
        self._order = tuple(slots.keys())
        # drop cooldowns for gone keys
        live = set(new_slots)
        self._failed = {k: v for k, v in self._failed.items() if k in live}
        if self._order:
            self._rr %= len(self._order)
        else:
            self._rr = 0
        return to_close

    async def update_slots(self, slots: dict[str, Provider]) -> None:
        async with self._lock:
            to_close = self._apply_slots_locked(slots)
        for provider in to_close:
            try:
                await provider.close()
            except Exception as e:
                logger.warning(f"{self.name}: close retired slot failed — {e}")

    def _mark_failed(self, key: str, generation: int) -> None:
        handle = self._slots.get(key)
        if (
            handle is None
            or handle.state != "active"
            or handle.generation != generation
        ):
            # Retired generations cannot mutate health for a replacement slot.
            return
        self._failed[key] = time.monotonic() + _COOLDOWN_S

    async def _acquire(self, tried: set[str], *, advance: bool) -> SlotHandle | None:
        async with self._lock:
            order = [k for k in self._order if k in self._slots]
            if not order:
                return None
            now = time.monotonic()
            n = len(order)
            start = self._rr % n
            if advance:
                self._rr = (self._rr + 1) % n
            ordered = order[start:] + order[:start]

            def _take(keys: list[str]) -> SlotHandle | None:
                for k in keys:
                    if k in tried:
                        continue
                    h = self._slots.get(k)
                    if h is None or h.state != "active":
                        continue
                    h.in_flight += 1
                    return h
                return None

            healthy = [k for k in ordered if self._failed.get(k, 0) <= now]
            handle = _take(healthy)
            if handle is not None:
                return handle
            # all cooling or tried — fall back to any untried active
            return _take(ordered)

    async def _release(self, handle: SlotHandle) -> None:
        should_close = False
        async with self._lock:
            handle.in_flight = max(0, handle.in_flight - 1)
            if handle.state == "retiring" and handle.in_flight == 0:
                # still not active under same object
                still_active = any(h is handle for h in self._slots.values())
                if not still_active:
                    should_close = True
        if should_close:
            try:
                await handle.provider.close()
            except Exception as e:
                logger.warning(f"{self.name}[{handle.key}]: drain close failed — {e}")

    async def complete(self, request: ChatCompletionRequest) -> dict:
        tried: set[str] = set()
        last_err: Exception | None = None
        advance = True
        while True:
            handle = await self._acquire(tried, advance=advance)
            advance = False
            if handle is None:
                if last_err is not None:
                    raise last_err
                raise ProviderUnavailableError(f"{self.name}: no available slots")
            tried.add(handle.key)
            try:
                result = await handle.provider.complete(request)
                request.record_slot(handle.key)
                return result
            except Exception as e:
                request.record_slot(handle.key)
                last_err = e
                logger.warning(f"{self.name}[{handle.key}]: complete failed — {e}")
                async with self._lock:
                    self._mark_failed(handle.key, handle.generation)
            finally:
                await self._release(handle)

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[bytes]:
        tried: set[str] = set()
        last_err: Exception | None = None
        advance = True
        while True:
            handle = await self._acquire(tried, advance=advance)
            advance = False
            if handle is None:
                if last_err is not None:
                    raise last_err
                raise ProviderUnavailableError(f"{self.name}: no available slots")
            tried.add(handle.key)
            committed = False
            try:
                async for chunk in handle.provider.stream(request):
                    if chunk:
                        committed = True
                        request.observe_stream_chunk(chunk)
                    request.record_slot(handle.key, committed=committed)
                    yield chunk
                return
            except Exception as e:
                request.record_slot(handle.key, committed=committed)
                async with self._lock:
                    self._mark_failed(handle.key, handle.generation)
                if committed:
                    raise
                last_err = e
                logger.warning(f"{self.name}[{handle.key}]: stream failed pre-commit — {e}")
            finally:
                await self._release(handle)

    async def close(self) -> None:
        async with self._lock:
            handles = list(self._slots.values())
            self._slots = {}
            self._order = ()
            for h in handles:
                h.state = "retiring"
            ready = [h for h in handles if h.in_flight == 0]
        for h in ready:
            try:
                await h.provider.close()
            except Exception as e:
                logger.warning(f"{self.name}[{h.key}]: close failed — {e}")


class LoadBalancedProvider(DynamicProviderPool):
    """Backward-compatible static N-instance wrapper around DynamicProviderPool."""

    def __init__(self, instances: list[Provider]):
        if not instances:
            raise ValueError("Need at least one provider instance")
        super().__init__(name=instances[0].name)
        # init is single-threaded; no concurrent acquire yet
        self._apply_slots_locked({str(i): p for i, p in enumerate(instances)})
