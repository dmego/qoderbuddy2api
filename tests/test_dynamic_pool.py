"""Tests for DynamicProviderPool: 0..N slots, stable-key cooldown, stream commit boundary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from qb2api.openai import ChatCompletionRequest
from qb2api.providers.base import Provider
from qb2api.providers.lb import DynamicProviderPool, ProviderUnavailableError


class FakeProvider(Provider):
    name = "fake"

    def __init__(self, account_id: str, *, fail_before_chunk: bool = False, fail_after_chunk: bool = False):
        self.account_id = account_id
        self.fail_before_chunk = fail_before_chunk
        self.fail_after_chunk = fail_after_chunk
        self.complete_calls = 0
        self.stream_calls = 0
        self.closed = False

    async def complete(self, request: ChatCompletionRequest) -> dict:
        self.complete_calls += 1
        if self.fail_before_chunk:
            raise RuntimeError(f"{self.account_id}-fail")
        return {"id": self.account_id, "choices": [{"message": {"role": "assistant", "content": self.account_id}}]}

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[bytes]:
        self.stream_calls += 1
        if self.fail_before_chunk:
            raise RuntimeError(f"{self.account_id}-pre")
        yield b"data: first\n\n"
        if self.fail_after_chunk:
            raise RuntimeError(f"{self.account_id}-post")
        yield b"data: [DONE]\n\n"

    async def close(self) -> None:
        self.closed = True


class DeferredFailureProvider(FakeProvider):
    def __init__(self, account_id: str) -> None:
        super().__init__(account_id)
        self.started = asyncio.Event()
        self.release_failure = asyncio.Event()

    async def complete(self, request: ChatCompletionRequest) -> dict:
        self.complete_calls += 1
        self.started.set()
        await self.release_failure.wait()
        raise RuntimeError(f"{self.account_id}-late-fail")


class BlockingStreamProvider(FakeProvider):
    def __init__(self, account_id: str) -> None:
        super().__init__(account_id)
        self.started = asyncio.Event()

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[bytes]:
        self.stream_calls += 1
        self.started.set()
        await asyncio.Event().wait()
        yield b"unreachable"


def _req() -> ChatCompletionRequest:
    return ChatCompletionRequest(model="m", messages=[{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_empty_pool_returns_unavailable():
    pool = DynamicProviderPool(name="codebuddy")
    with pytest.raises(ProviderUnavailableError):
        await pool.complete(_req())


@pytest.mark.asyncio
async def test_round_robin_across_slots():
    a = FakeProvider("a")
    b = FakeProvider("b")
    pool = DynamicProviderPool(name="codebuddy")
    await pool.update_slots({"a": a, "b": b})
    r1 = await pool.complete(_req())
    r2 = await pool.complete(_req())
    assert {r1["id"], r2["id"]} == {"a", "b"}


@pytest.mark.asyncio
async def test_failover_before_commit_uses_next_slot():
    bad = FakeProvider("bad", fail_before_chunk=True)
    good = FakeProvider("good")
    pool = DynamicProviderPool(name="codebuddy")
    await pool.update_slots({"bad": bad, "good": good})
    result = await pool.complete(_req())
    assert result["id"] == "good"
    assert bad.complete_calls == 1
    assert good.complete_calls == 1


@pytest.mark.asyncio
async def test_stream_no_failover_after_first_chunk():
    first = FakeProvider("first", fail_after_chunk=True)
    second = FakeProvider("second")
    pool = DynamicProviderPool(name="codebuddy")
    await pool.update_slots({"first": first, "second": second})
    chunks = []
    with pytest.raises(RuntimeError, match="first-post"):
        async for c in pool.stream(_req()):
            chunks.append(c)
    assert chunks == [b"data: first\n\n"]
    assert second.stream_calls == 0


@pytest.mark.asyncio
async def test_update_slots_to_zero_then_unavailable():
    a = FakeProvider("a")
    pool = DynamicProviderPool(name="codebuddy")
    await pool.update_slots({"a": a})
    assert await pool.complete(_req())
    await pool.update_slots({})
    with pytest.raises(ProviderUnavailableError):
        await pool.complete(_req())


@pytest.mark.asyncio
async def test_retiring_provider_not_closed_while_in_flight():
    slow = FakeProvider("slow")

    async def slow_stream(request):
        slow.stream_calls += 1
        yield b"data: keep\n\n"
        await asyncio.sleep(0.05)
        yield b"data: [DONE]\n\n"

    slow.stream = slow_stream  # type: ignore[method-assign]
    pool = DynamicProviderPool(name="codebuddy")
    await pool.update_slots({"slow": slow})

    agen = pool.stream(_req())
    first = await agen.__anext__()
    assert first.startswith(b"data:")
    await pool.update_slots({})
    # still in flight — not closed yet
    assert slow.closed is False
    # drain remaining
    async for _ in agen:
        pass
    await asyncio.sleep(0.01)
    assert slow.closed is True


@pytest.mark.asyncio
async def test_retired_generation_failure_does_not_cool_replacement():
    old = DeferredFailureProvider("old")
    replacement = FakeProvider("replacement")
    fallback = FakeProvider("fallback")
    pool = DynamicProviderPool(name="codebuddy")
    await pool.update_slots({"account": old})

    request = asyncio.create_task(pool.complete(_req()))
    await old.started.wait()
    await pool.update_slots({})
    old.release_failure.set()
    with pytest.raises(RuntimeError, match="old-late-fail"):
        await request

    await pool.update_slots({"account": replacement, "fallback": fallback})
    result = await pool.complete(_req())
    assert result["id"] == "replacement"
    assert replacement.complete_calls == 1
    assert fallback.complete_calls == 0


@pytest.mark.asyncio
async def test_stream_cancellation_releases_retired_slot():
    provider = BlockingStreamProvider("cancelled")
    pool = DynamicProviderPool(name="codebuddy")
    await pool.update_slots({"account": provider})

    async def consume() -> None:
        async for _ in pool.stream(_req()):
            pass

    task = asyncio.create_task(consume())
    await provider.started.wait()
    await pool.update_slots({})
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert provider.closed is True


@pytest.mark.asyncio
async def test_qoder_provider_authenticates_once_per_account_under_concurrency(
    monkeypatch,
):
    from qb2api.providers.qoder import QoderProvider, QoderSession

    authentications: list[str] = []

    async def authenticate(session: QoderSession) -> None:
        authentications.append(session.pat)
        await asyncio.sleep(0)
        session.user_id = f"user-{session.pat}"
        session._ready = True

    monkeypatch.setattr(QoderSession, "authenticate", authenticate)
    first = QoderProvider(pat="pat-a")
    second = QoderProvider(pat="pat-b")
    try:
        first_a, first_b, second_session = await asyncio.gather(
            first._ensure_session(),
            first._ensure_session(),
            second._ensure_session(),
        )
    finally:
        await first.close()
        await second.close()

    assert first_a is first_b
    assert first_a is not second_session
    assert authentications.count("pat-a") == 1
    assert authentications.count("pat-b") == 1


@pytest.mark.asyncio
async def test_qoder_provider_reauthenticates_once_before_first_chunk(monkeypatch):
    from qb2api.providers.qoder import QoderError, QoderProvider, QoderSession

    sessions: list[QoderSession] = []
    attempts: list[QoderSession] = []

    async def authenticate(session: QoderSession) -> None:
        session.user_id = "user"
        session._ready = True
        sessions.append(session)

    async def stream_once(
        _provider: QoderProvider,
        _request: ChatCompletionRequest,
        session: QoderSession,
    ) -> AsyncIterator[bytes]:
        attempts.append(session)
        if len(attempts) == 1:
            raise QoderError("expired", status_code=401)
        yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'

    monkeypatch.setattr(QoderSession, "authenticate", authenticate)
    monkeypatch.setattr(QoderProvider, "_stream_once", stream_once)
    provider = QoderProvider(pat="pat-a")
    try:
        chunks = [chunk async for chunk in provider.stream(_req())]
    finally:
        await provider.close()

    assert len(sessions) == 2
    assert attempts == sessions
    assert chunks[-1] == b"data: [DONE]\n\n"
