"""Tests for DynamicProviderPool: 0..N slots, stable-key cooldown, stream commit boundary."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from qb2api.openai import ChatCompletionRequest
from qb2api.providers.base import Provider
from qb2api.providers.codebuddy import CodeBuddyQuotaExceededError
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


class QuotaFailProvider(FakeProvider):
    def __init__(self, account_id: str, reset_at: datetime, *, fail_times: int = 1 << 30) -> None:
        super().__init__(account_id)
        self._reset_at = reset_at
        self._fail_times = fail_times

    async def complete(self, request: ChatCompletionRequest) -> dict:
        self.complete_calls += 1
        if self.complete_calls <= self._fail_times:
            raise CodeBuddyQuotaExceededError(429, "您的使用量已超出频率限制", self._reset_at)
        return {"id": self.account_id, "choices": [{"message": {"role": "assistant", "content": self.account_id}}]}


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


@pytest.mark.asyncio
async def test_quota_error_blocks_only_affected_model():
    quota = QuotaFailProvider("a", datetime.now(UTC) + timedelta(seconds=120))
    healthy = FakeProvider("healthy")
    pool = DynamicProviderPool(name="codebuddy")
    await pool.update_slots({"a": quota, "b": healthy})

    assert (await pool.complete(_req()))["id"] == "healthy"
    assert (await pool.complete(_req()))["id"] == "healthy"
    assert quota.complete_calls == 1  # blocked for the model, not retried

    healthy.fail_before_chunk = True
    with pytest.raises(RuntimeError):  # pool surfaces the last attempt's error
        await pool.complete(ChatCompletionRequest(model="other", messages=[{"role": "user", "content": "hi"}]))
    assert quota.complete_calls == 2  # retried for a different model: block is model-scoped
    assert "a" not in pool._failed  # no account-wide cooldown for quota errors
    assert set(pool._model_blocked) == {("a", "m"), ("a", "other")}


@pytest.mark.asyncio
async def test_quota_block_releases_after_reset(monkeypatch: pytest.MonkeyPatch):
    quota = QuotaFailProvider("a", datetime.now(UTC) + timedelta(seconds=45), fail_times=1)
    pool = DynamicProviderPool(name="codebuddy")
    await pool.update_slots({"a": quota})

    with pytest.raises(CodeBuddyQuotaExceededError):
        await pool.complete(_req())
    base = time.monotonic()
    monkeypatch.setattr("qb2api.providers.lb.time.monotonic", lambda: base + 60)
    assert quota.complete_calls == 1

    assert (await pool.complete(_req()))["id"] == "a"
    assert quota.complete_calls == 2


@pytest.mark.asyncio
async def test_generic_error_keeps_account_cooldown():
    flaky = FakeProvider("flaky", fail_before_chunk=True)
    good = FakeProvider("good")
    pool = DynamicProviderPool(name="codebuddy")
    await pool.update_slots({"flaky": flaky, "good": good})

    assert (await pool.complete(_req()))["id"] == "good"
    assert set(pool._failed) == {"flaky"}
    assert pool._model_blocked == {}


def test_fallback_prefers_earliest_reset():
    pool = DynamicProviderPool(name="codebuddy")
    pool._apply_slots_locked({"a": FakeProvider("a"), "b": FakeProvider("b")})
    now = time.monotonic()
    pool._model_blocked[("a", "m")] = now + 100
    pool._model_blocked[("b", "m")] = now + 50

    assert pool._available_handle(["a", "b"], set(), "m").key == "b"


@pytest.mark.asyncio
async def test_quota_blocks_snapshot_reports_active_blocks():
    pool = DynamicProviderPool(name="codebuddy")
    await pool.update_slots({"codebuddy:cb-1": FakeProvider("cb-1")})
    pool._mark_model_blocked("codebuddy:cb-1", "deepseek-v4.1-flash", datetime.now(UTC) + timedelta(seconds=90))

    blocks = pool.quota_blocks()
    assert len(blocks) == 1
    assert blocks[0]["provider"] == "codebuddy"
    assert blocks[0]["account_id"] == "cb-1"
    assert blocks[0]["model_id"] == "deepseek-v4.1-flash"
    datetime.fromisoformat(blocks[0]["blocked_until"].replace("Z", "+00:00"))


@pytest.mark.asyncio
async def test_update_slots_purges_model_blocks_for_retired_slots():
    pool = DynamicProviderPool(name="codebuddy")
    account = FakeProvider("a")
    await pool.update_slots({"a": account})
    pool._mark_model_blocked("a", "m", datetime.now(UTC) + timedelta(seconds=90))
    assert pool._model_blocked

    await pool.update_slots({})
    assert pool._model_blocked == {}
