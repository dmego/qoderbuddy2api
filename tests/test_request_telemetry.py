"""Provider pool records stable account and real upstream usage."""

from __future__ import annotations

import pytest

from qb2api.openai import ChatCompletionRequest
from qb2api.providers.base import Provider
from qb2api.providers.lb import DynamicProviderPool


class StreamProvider(Provider):
    name = "codebuddy"

    async def complete(self, request):
        return {"usage": {"prompt_tokens": 3, "completion_tokens": 2}}

    async def stream(self, request):
        yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        yield b'data: {"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
        yield b"data: [DONE]\n\n"

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_stream_records_slot_commit_and_real_usage():
    pool = DynamicProviderPool("codebuddy")
    await pool.update_slots({"codebuddy:cb-1": StreamProvider()})
    request = ChatCompletionRequest(model="m", messages=[], stream=True)
    chunks = [chunk async for chunk in pool.stream(request)]
    assert chunks
    assert request.telemetry == {
        "account_id": "cb-1",
        "stream_committed": True,
        "input_tokens": 3,
        "output_tokens": 2,
    }
    await pool.close()
