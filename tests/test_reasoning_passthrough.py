"""Tests for streaming reasoning_content passthrough switch (Task 6)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from unittest.mock import Mock

import pytest

from qb2api.config import Settings
from qb2api.worker.streaming import StreamLogContext, openai_stream


def _async_iter(chunks: list[bytes | str]) -> AsyncIterator[bytes | str]:
    async def gen() -> AsyncIterator[bytes | str]:
        for chunk in chunks:
            yield chunk

    return gen()


def _provider(chunks: list[bytes | str]) -> Mock:
    provider = Mock()
    provider.stream.return_value = _async_iter(chunks)
    return provider


def _context() -> StreamLogContext:
    return StreamLogContext(provider_name="qoder", model="m", reasoning_effort=None, tool_calls_count=0)


async def _run_openai_stream(
    provider: Mock,
    *,
    keep_reasoning: bool | None = None,
    settings: object | None = None,
) -> list[bytes]:
    return [
        chunk
        async for chunk in openai_stream(
            provider=provider,
            request=Mock(model="m"),
            context=_context(),
            request_logger=None,
            keep_reasoning=keep_reasoning,
            settings=settings,
        )
    ]


def _chunks(chunks: list[bytes]) -> list[dict]:
    return [
        json.loads(c[6:].decode().strip())
        for c in chunks
        if c.startswith(b"data: ") and c.strip() != b"data: [DONE]"
    ]


_REASONING_CHUNK = b'data: {"choices":[{"delta":{"reasoning_content":"think...","content":"hi"}}]}\n\n'


async def test_stream_reasoning_stripped_by_default() -> None:
    out = await _run_openai_stream(_provider([_REASONING_CHUNK, b"data: [DONE]\n\n"]))
    chunks = _chunks(out)
    assert len(chunks) == 1
    assert "reasoning_content" not in chunks[0]["choices"][0]["delta"]
    assert chunks[0]["choices"][0]["delta"]["content"] == "hi"


async def test_stream_reasoning_kept_when_keep_reasoning_true() -> None:
    out = await _run_openai_stream(_provider([_REASONING_CHUNK, b"data: [DONE]\n\n"]), keep_reasoning=True)
    chunks = _chunks(out)
    assert chunks[0]["choices"][0]["delta"]["reasoning_content"] == "think..."
    assert chunks[0]["choices"][0]["delta"]["content"] == "hi"


async def test_stream_reasoning_stripped_when_keep_reasoning_false() -> None:
    out = await _run_openai_stream(_provider([_REASONING_CHUNK]), keep_reasoning=False)
    chunks = _chunks(out)
    assert "reasoning_content" not in chunks[0]["choices"][0]["delta"]


async def test_stream_reasoning_kept_via_settings_flag() -> None:
    out = await _run_openai_stream(_provider([_REASONING_CHUNK]), settings=Settings(stream_reasoning=True))
    chunks = _chunks(out)
    assert chunks[0]["choices"][0]["delta"]["reasoning_content"] == "think..."


async def test_stream_reasoning_stripped_via_settings_default() -> None:
    out = await _run_openai_stream(_provider([_REASONING_CHUNK]), settings=Settings(stream_reasoning=False))
    chunks = _chunks(out)
    assert "reasoning_content" not in chunks[0]["choices"][0]["delta"]


async def test_keep_reasoning_overrides_settings() -> None:
    # Explicit flag wins over settings.stream_reasoning.
    out = await _run_openai_stream(
        _provider([_REASONING_CHUNK]),
        keep_reasoning=False,
        settings=Settings(stream_reasoning=True),
    )
    chunks = _chunks(out)
    assert "reasoning_content" not in chunks[0]["choices"][0]["delta"]


async def test_done_marker_passes_through_unchanged() -> None:
    out = await _run_openai_stream(_provider([_REASONING_CHUNK, b"data: [DONE]\n\n"]))
    assert out[-1] == b"data: [DONE]\n\n"


async def test_non_data_line_passes_through_unchanged() -> None:
    out = await _run_openai_stream(_provider([b"event: ping\n\n", _REASONING_CHUNK]))
    assert out[0] == b"event: ping\n\n"


async def test_json_parse_failure_passes_through_unchanged() -> None:
    raw = b"data: not-json\n\n"
    out = await _run_openai_stream(_provider([raw]))
    assert out[0] == raw


async def test_error_chunk_without_choices_passes_through() -> None:
    raw = b'data: {"error":{"message":"boom","type":"upstream_error"}}\n\n'
    out = await _run_openai_stream(_provider([raw]))
    assert json.loads(out[0][6:].strip()) == {"error": {"message": "boom", "type": "upstream_error"}}


async def test_str_chunk_is_encoded_and_filtered() -> None:
    out = await _run_openai_stream(_provider(["data: {bad json", _REASONING_CHUNK.decode()]))
    assert isinstance(out[0], bytes)
    assert isinstance(out[1], bytes)
    assert b"reasoning_content" not in out[1]


async def test_anthropic_stream_forwards_thinking_block() -> None:
    """Anthropic SSE conversion exposes upstream reasoning as a thinking block."""
    from qb2api.anthropic_stream import openai_stream_to_anthropic

    async def gen() -> AsyncIterator[bytes]:
        yield _REASONING_CHUNK
        yield b"data: [DONE]\n\n"

    events = [event async for event in openai_stream_to_anthropic(gen(), model="hy3")]
    text = b"".join(events).decode()
    assert "content_block_start" in text
    assert '"type":"thinking"' in text or '"type": "thinking"' in text
    assert "think..." in text


async def test_anthropic_nonstream_includes_thinking() -> None:
    """Non-streaming Anthropic response includes the reasoning as thinking."""
    from qb2api.anthropic import openai_to_anthropic

    response = {
        "id": "chatcmpl-1",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "hi",
                "reasoning_content": "think...",
            },
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    out = openai_to_anthropic(response, model="hy3")
    assert out["content"][0]["type"] == "thinking"
    assert out["content"][0]["thinking"] == "think..."
    assert out["content"][1]["type"] == "text"


def test_config_env_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QB2API_STREAM_REASONING", raising=False)
    # Default flips reasoning passthrough ON so thinking models expose steps.
    assert Settings.from_env(env_file="").stream_reasoning is True

    monkeypatch.setenv("QB2API_STREAM_REASONING", "1")
    assert Settings.from_env(env_file="").stream_reasoning is True

    monkeypatch.setenv("QB2API_STREAM_REASONING", "false")
    assert Settings.from_env(env_file="").stream_reasoning is False

    monkeypatch.setenv("QB2API_CODEBUDDY_DEFAULT_REASONING_EFFORT", "high")
    assert Settings.from_env(env_file="").codebuddy_default_reasoning_effort == "high"

    monkeypatch.delenv("QB2API_CODEBUDDY_DEFAULT_REASONING_EFFORT", raising=False)
    assert Settings.from_env(env_file="").codebuddy_default_reasoning_effort == "max"
