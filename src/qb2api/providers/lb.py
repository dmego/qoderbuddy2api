"""Dynamic provider pool with stable-key RR, drain, and stream commit."""

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
    pass


class _PrecommitStreamFailure(Exception):
    def __init__(self, error: Exception) -> None:
        self.error = error
        super().__init__(str(error))


@dataclass
class SlotHandle:
    key: str
    provider: Provider
    state: Literal["active", "retiring"] = "active"
    in_flight: int = 0
    generation: int = 0


SlotMap = dict[str, SlotHandle]


class DynamicProviderPool(Provider):
    """Stable 0..N pool: RR by slot key, failover pre-commit, lease drain on retire."""

    def __init__(self, name: str):
        self.name = name
        self._lock = asyncio.Lock()
        self._slots: dict[str, SlotHandle] = {}
        self._order: tuple[str, ...] = ()
        self._rr = 0
        self._failed: dict[str, float] = {}
        self._gen = 0

    @property
    def has_available_slots(self) -> bool:
        return bool(self._slots)

    @property
    def instance_count(self) -> int:
        return len(self._slots)

    def _apply_slots_locked(self, slots: dict[str, Provider]) -> list[Provider]:
        new_slots, to_close = self._build_slot_snapshot(slots)
        self._retire_missing_slots(new_slots, to_close)
        self._commit_slot_snapshot(slots, new_slots)
        return to_close

    def _build_slot_snapshot(self, slots: dict[str, Provider]) -> tuple[SlotMap, list[Provider]]:
        new_slots: SlotMap = {}
        to_close: list[Provider] = []
        for key, provider in slots.items():
            old = self._slots.get(key)
            if self._reuse_slot(old, provider):
                assert old is not None
                new_slots[key] = old
                continue
            self._retire_replaced_slot(old, to_close)
            new_slots[key] = self._new_slot(key, provider)
        return new_slots, to_close

    def _reuse_slot(self, old: SlotHandle | None, provider: Provider) -> bool:
        if old is None or old.provider is not provider:
            return False
        old.state = "active"
        return True

    def _retire_replaced_slot(self, old: SlotHandle | None, to_close: list[Provider]) -> None:
        if old is not None:
            old.state = "retiring"
            if old.in_flight == 0:
                to_close.append(old.provider)

    def _new_slot(self, key: str, provider: Provider) -> SlotHandle:
        self._gen += 1
        return SlotHandle(key=key, provider=provider, state="active", generation=self._gen)

    def _retire_missing_slots(self, new_slots: SlotMap, to_close: list[Provider]) -> None:
        for key, old in self._slots.items():
            if key in new_slots and new_slots[key] is old:
                continue
            self._retire_missing_slot(old, new_slots, to_close)

    @staticmethod
    def _retire_missing_slot(old: SlotHandle, new_slots: SlotMap, to_close: list[Provider]) -> None:
        old.state = "retiring"
        if (
            old.in_flight != 0
            or old.provider in to_close
            or any(handle.provider is old.provider for handle in new_slots.values())
        ):
            return
        to_close.append(old.provider)

    def _commit_slot_snapshot(
        self,
        slots: dict[str, Provider],
        new_slots: SlotMap,
    ) -> None:
        self._slots = new_slots
        self._order = tuple(slots.keys())
        live = set(new_slots)
        self._failed = {k: v for k, v in self._failed.items() if k in live}
        self._rr = self._rr % len(self._order) if self._order else 0

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
            return
        self._failed[key] = time.monotonic() + _COOLDOWN_S

    async def _acquire(self, tried: set[str], *, advance: bool) -> SlotHandle | None:
        async with self._lock:
            order = self._live_slot_order()
            if not order:
                return None
            ordered = self._round_robin_order(order, advance=advance)
            return self._available_handle(ordered, tried)

    def _live_slot_order(self) -> list[str]:
        return [key for key in self._order if key in self._slots]

    def _round_robin_order(self, order: list[str], *, advance: bool) -> list[str]:
        count = len(order)
        start = self._rr % count
        if advance:
            self._rr = (self._rr + 1) % count
        return order[start:] + order[:start]

    def _available_handle(
        self,
        ordered: list[str],
        tried: set[str],
    ) -> SlotHandle | None:
        now = time.monotonic()
        healthy = [key for key in ordered if self._failed.get(key, 0) <= now]
        return self._take_handle(healthy, tried) or self._take_handle(ordered, tried)

    def _take_handle(
        self,
        keys: list[str],
        tried: set[str],
    ) -> SlotHandle | None:
        for key in keys:
            if key in tried:
                continue
            handle = self._slots.get(key)
            if handle is None or handle.state != "active":
                continue
            handle.in_flight += 1
            return handle
        return None

    async def _release(self, handle: SlotHandle) -> None:
        should_close = False
        async with self._lock:
            handle.in_flight = max(0, handle.in_flight - 1)
            if handle.state == "retiring" and handle.in_flight == 0:
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
            try:
                async for chunk in self._stream_handle(handle, request):
                    yield chunk
                return
            except _PrecommitStreamFailure as failure:
                async with self._lock:
                    self._mark_failed(handle.key, handle.generation)
                last_err = failure.error
                logger.warning(f"{self.name}[{handle.key}]: stream failed pre-commit — {failure.error}")
            except Exception:
                async with self._lock:
                    self._mark_failed(handle.key, handle.generation)
                raise
            finally:
                await self._release(handle)

    async def _stream_handle(
        self,
        handle: SlotHandle,
        request: ChatCompletionRequest,
    ) -> AsyncIterator[bytes]:
        committed = False
        try:
            async for chunk in handle.provider.stream(request):
                if chunk:
                    committed = True
                    request.observe_stream_chunk(chunk)
                request.record_slot(handle.key, committed=committed)
                yield chunk
        except Exception as error:
            request.record_slot(handle.key, committed=committed)
            if committed:
                raise
            raise _PrecommitStreamFailure(error) from error

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
        self._apply_slots_locked({str(i): p for i, p in enumerate(instances)})
