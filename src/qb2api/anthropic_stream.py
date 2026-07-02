"""Anthropic Messages SSE conversion."""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from .anthropic import map_finish_reason


async def openai_stream_to_anthropic(stream: AsyncIterator[bytes], model: str) -> AsyncIterator[bytes]:
    """Convert OpenAI chat-completion SSE chunks into Anthropic Messages SSE events."""
    message_id = f"msg_{uuid.uuid4().hex[:24]}"
    yield _sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )

    state = _StreamState()
    async for raw_chunk in stream:
        for event in state.process_raw_chunk(raw_chunk):
            yield event

    if not state.stopped:
        for event in state.stop("end_turn"):
            yield event


def anthropic_error_sse(message: str) -> bytes:
    """Create an Anthropic-compatible SSE error event."""
    return _sse(
        "error",
        {
            "type": "error",
            "error": {"type": "api_error", "message": message},
        },
    )


class _StreamState:
    def __init__(self) -> None:
        self.active_blocks: list[int] = []
        self.text_index: int | None = None
        self.tool_indexes: dict[int, int] = {}
        self.next_index = 0
        self.stopped = False
        self._buf = ""  # ponytail: buffer partial SSE lines across TCP chunks

    def process_raw_chunk(self, raw_chunk: bytes) -> list[bytes]:
        events: list[bytes] = []
        text = self._buf + raw_chunk.decode("utf-8", errors="replace")
        # Preserve trailing partial line — SSE frame may span TCP chunks
        if not text.endswith("\n"):
            *complete, self._buf = text.split("\n")
        else:
            complete = text.splitlines()
            self._buf = ""
        for line in complete:
            if not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if data == "[DONE]":
                events.extend(self.stop("end_turn"))
                continue
            chunk = _load_json(data)
            if chunk is not None:
                events.extend(self.process_chunk(chunk))
        return events

    def process_chunk(self, chunk: dict[str, Any]) -> list[bytes]:
        choices = chunk.get("choices") or []
        if not choices:
            return []
        choice = choices[0]
        delta = choice.get("delta") or {}
        events = self.process_delta(delta)
        finish_reason = choice.get("finish_reason")
        if finish_reason:
            events.extend(self.stop(map_finish_reason(finish_reason)))
        return events

    def process_delta(self, delta: dict[str, Any]) -> list[bytes]:
        events: list[bytes] = []
        if delta.get("content"):
            events.extend(self._text_delta(delta["content"]))
        for tool_call in delta.get("tool_calls") or []:
            events.extend(self._tool_delta(tool_call))
        return events

    def stop(self, stop_reason: str | None) -> list[bytes]:
        if self.stopped:
            return []
        events = [
            _sse("content_block_stop", {"type": "content_block_stop", "index": index})
            for index in self.active_blocks
        ]
        self.active_blocks.clear()
        events.append(
            _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": 0},
                },
            )
        )
        events.append(_sse("message_stop", {"type": "message_stop"}))
        self.stopped = True
        return events

    def _text_delta(self, text: str) -> list[bytes]:
        events: list[bytes] = []
        if self.text_index is None:
            self.text_index = self._reserve_block()
            events.append(
                _sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": self.text_index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            )
        events.append(
            _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self.text_index,
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        )
        return events

    def _tool_delta(self, tool_call: dict[str, Any]) -> list[bytes]:
        openai_index = int(tool_call.get("index") or 0)
        function = tool_call.get("function") or {}
        events: list[bytes] = []
        if openai_index not in self.tool_indexes:
            self.tool_indexes[openai_index] = self._reserve_block()
            events.append(_tool_start_event(self.tool_indexes[openai_index], openai_index, tool_call, function))
        if function.get("arguments"):
            events.append(_tool_args_event(self.tool_indexes[openai_index], function["arguments"]))
        return events

    def _reserve_block(self) -> int:
        index = self.next_index
        self.next_index += 1
        self.active_blocks.append(index)
        return index


def _tool_start_event(index: int, openai_index: int, tool_call: dict[str, Any], function: dict[str, Any]) -> bytes:
    return _sse(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {
                "type": "tool_use",
                "id": tool_call.get("id") or f"toolu_{uuid.uuid4().hex[:16]}",
                "name": function.get("name") or f"tool_{openai_index}",
                "input": {},
            },
        },
    )


def _tool_args_event(index: int, arguments: str) -> bytes:
    return _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": arguments},
        },
    )


def _load_json(data: str) -> dict[str, Any] | None:
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


def _sse(event: str, data: dict[str, Any]) -> bytes:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode()
