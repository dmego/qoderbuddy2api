"""Dynamic provider pool with stable-key RR, drain, and stream commit."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from ..openai import ChatCompletionRequest
from .base import Provider

logger = logging.getLogger("qb2api")

_COOLDOWN_S = 30.0
# 6004 carries the upstream reset wall clock; trust it but bound pathological
# values. Unparseable reset falls back to the plain cooldown window.
_MODEL_BLOCK_MAX_S = 24 * 3600.0
_MODEL_BLOCK_DEFAULT_S = _COOLDOWN_S


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
        # Auto blocks are timed and self-clearing; hard blocks are administrator
        # decisions that only an explicit unblock removes.
        self._model_blocked: dict[tuple[str, str], float] = {}
        self._model_block_reason: dict[tuple[str, str], str] = {}
        self._model_blocked_hard: set[tuple[str, str]] = set()
        self._model_hard_reason: dict[tuple[str, str], str] = {}
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
        self._purge_retired_blocks(live)
        self._rr = self._rr % len(self._order) if self._order else 0

    async def update_slots(self, slots: dict[str, Provider]) -> None:
        async with self._lock:
            to_close = self._apply_slots_locked(slots)
        for provider in to_close:
            try:
                await provider.close()
            except Exception as e:
                logger.warning(f"{self.name}: close retired slot failed — {e}")

    def _purge_retired_blocks(self, live: set[str]) -> None:
        """Drop block bookkeeping for slots that no longer exist."""
        self._model_blocked = {
            blocked: until for blocked, until in self._model_blocked.items() if blocked[0] in live
        }
        self._model_block_reason = {
            blocked: reason
            for blocked, reason in self._model_block_reason.items()
            if blocked[0] in live
        }
        self._model_blocked_hard = {
            blocked for blocked in self._model_blocked_hard if blocked[0] in live
        }
        self._model_hard_reason = {
            blocked: reason
            for blocked, reason in self._model_hard_reason.items()
            if blocked[0] in live
        }

    def _mark_failed(self, key: str, generation: int) -> None:
        handle = self._slots.get(key)
        if (
            handle is None
            or handle.state != "active"
            or handle.generation != generation
        ):
            return
        self._failed[key] = time.monotonic() + _COOLDOWN_S

    def _mark_outcome(
        self,
        handle: SlotHandle,
        model: str,
        error: Exception,
    ) -> None:
        """Record a failed attempt as a model-scoped quota block or generic cooldown.

        Quota errors (duck-typed via ``model_block_reset_at``, e.g. CodeBuddy
        6004) only block this account for the affected model — upstream
        explicitly allows switching models on the same account. Everything
        else keeps the account-wide cooldown.
        """
        reset_at = getattr(error, "model_block_reset_at", None)
        if model and isinstance(reset_at, datetime):
            self._mark_model_blocked(
                handle.key, model, reset_at, reason=_error_reason(error)
            )
            return
        self._mark_failed(handle.key, handle.generation)

    def _mark_model_blocked(
        self,
        key: str,
        model: str,
        reset_at: datetime,
        *,
        reason: str = "",
    ) -> None:
        now = time.monotonic()
        delay = min(max(reset_at.timestamp() - time.time(), _MODEL_BLOCK_DEFAULT_S), _MODEL_BLOCK_MAX_S)
        self._model_blocked[(key, model)] = now + delay
        if reason:
            self._model_block_reason[(key, model)] = reason
        if len(self._model_blocked) > 1:
            self._model_blocked = {
                blocked: until
                for blocked, until in self._model_blocked.items()
                if until > now or blocked == (key, model)
            }
            self._model_block_reason = {
                blocked: text
                for blocked, text in self._model_block_reason.items()
                if blocked in self._model_blocked
            }

    def set_hard_blocks(self, blocks: dict[tuple[str, str], str]) -> None:
        """Replace the administrator's manual blocks.

        Hard blocks survive cooldown expiry and are not canaried by the
        fallback path — the operator asked for this account to stay off this
        model until they say otherwise.
        """
        self._model_blocked_hard = set(blocks)
        self._model_hard_reason = dict(blocks)

    async def _acquire(self, tried: set[str], *, advance: bool, model: str) -> SlotHandle | None:
        async with self._lock:
            order = self._live_slot_order()
            if not order:
                return None
            ordered = self._round_robin_order(order, advance=advance)
            return self._available_handle(ordered, tried, model)

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
        model: str,
    ) -> SlotHandle | None:
        now = time.monotonic()
        # A manually blocked account is never a candidate, not even as a last
        # resort; everything else degrades to the fallback path below.
        candidates = [key for key in ordered if (key, model) not in self._model_blocked_hard]
        healthy = [
            key
            for key in candidates
            if self._failed.get(key, 0) <= now
            and self._model_blocked.get((key, model), 0) <= now
        ]
        if healthy:
            return self._take_handle(healthy, tried)
        # Everything is cooling or auto-blocked for this model: fall back to the
        # remaining order so a recovered account gets canaried, earliest reset
        # first (most likely to succeed).
        fallback = sorted(
            candidates,
            key=lambda key: (
                self._model_blocked.get((key, model), 0),
                self._failed.get(key, 0),
            ),
        )
        return self._take_handle(fallback, tried)

    def quota_blocks(self) -> list[dict[str, str]]:
        """Snapshot of active model-scoped blocks for observability.

        Covers both auto blocks (upstream quota, self-clearing) and manual
        blocks (administrator decision), each with the reason to display.
        """
        now = time.monotonic()
        now_wall = datetime.now(UTC)
        blocks = []
        for (key, model), until in self._model_blocked.items():
            if until <= now:
                continue
            provider, account_id = self._split_slot_key(key)
            blocked_until = now_wall + timedelta(seconds=until - now)
            blocks.append({
                "provider": provider,
                "account_id": account_id,
                "model_id": model,
                "blocked_until": blocked_until.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "reason": self._model_block_reason.get((key, model), ""),
                "source": "auto",
            })
        for key, model in self._model_blocked_hard:
            provider, account_id = self._split_slot_key(key)
            blocks.append({
                "provider": provider,
                "account_id": account_id,
                "model_id": model,
                "blocked_until": "",
                "reason": self._model_hard_reason.get((key, model), ""),
                "source": "manual",
            })
        return blocks

    def _split_slot_key(self, key: str) -> tuple[str, str]:
        """Split a slot key into (provider, account_id).

        Slot keys are normally ``provider:account_id``; a key without the prefix
        falls back to this pool's own provider name rather than reporting an
        empty account in the observability snapshot.
        """
        provider, separator, account_id = key.partition(":")
        if not separator:
            return self.name, key
        return provider, account_id

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
            handle = await self._acquire(tried, advance=advance, model=request.model)
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
                    self._mark_outcome(handle, request.model, e)
            finally:
                await self._release(handle)

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[bytes]:
        tried: set[str] = set()
        last_err: Exception | None = None
        advance = True
        while True:
            handle = await self._acquire(tried, advance=advance, model=request.model)
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
                    self._mark_outcome(handle, request.model, failure.error)
                last_err = failure.error
                logger.warning(f"{self.name}[{handle.key}]: stream failed pre-commit — {failure.error}")
            except Exception as error:
                async with self._lock:
                    self._mark_outcome(handle, request.model, error)
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


def _error_reason(error: Exception) -> str:
    """Short, secret-free explanation of why an account was auto-blocked."""
    message = getattr(error, "message", None) or str(error)
    return " ".join(str(message).split())[:200]
