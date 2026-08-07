"""Streaming behavior at the provider failover commit boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from qb2api.openai import ChatCompletionRequest
from qb2api.providers.base import Provider
from qb2api.providers.lb import DynamicProviderPool
from qb2api.worker.streaming import StreamLogContext, openai_stream


class _StreamingProvider(Provider):
    name = "codebuddy"

    def __init__(self, label: str, *, fail_after_first: bool = False) -> None:
        self.label = label
        self.fail_after_first = fail_after_first
        self.calls = 0

    async def complete(self, request: ChatCompletionRequest) -> dict:
        self.calls += 1
        return {"id": self.label}

    async def stream(
        self,
        request: ChatCompletionRequest,
    ) -> AsyncIterator[bytes]:
        self.calls += 1
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        if self.fail_after_first:
            raise RuntimeError("post-commit failure")
        yield b"data: [DONE]\n\n"

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_post_commit_failure_is_not_replayed_or_marked_done() -> None:
    first = _StreamingProvider("first", fail_after_first=True)
    second = _StreamingProvider("second")
    pool = DynamicProviderPool("codebuddy")
    await pool.update_slots({"codebuddy:a": first, "codebuddy:b": second})
    request = ChatCompletionRequest(
        model="test-model",
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
    )

    context = StreamLogContext(
        provider_name="codebuddy",
        model="codebuddy/test-model",
        reasoning_effort=None,
        tool_calls_count=0,
    )
    chunks = [
        chunk
        async for chunk in openai_stream(
            provider=pool,
            request=request,
            context=context,
            request_logger=None,
        )
    ]

    assert second.calls == 0
    assert b"partial" in b"".join(chunks)
    assert b"[DONE]" not in b"".join(chunks)


@pytest.mark.asyncio
async def test_post_commit_failure_cools_only_failed_slot() -> None:
    first = _StreamingProvider("first", fail_after_first=True)
    second = _StreamingProvider("second")
    pool = DynamicProviderPool("codebuddy")
    await pool.update_slots({"codebuddy:a": first, "codebuddy:b": second})
    request = ChatCompletionRequest(
        model="test-model",
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
    )

    with pytest.raises(RuntimeError, match="post-commit failure"):
        async for _ in pool.stream(request):
            pass

    first_result = await pool.complete(request)
    second_result = await pool.complete(request)

    assert first_result["id"] == "second"
    assert second_result["id"] == "second"
