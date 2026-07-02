"""SSE parsing and stream aggregation."""

import json
import time
import logging
from typing import AsyncIterator

logger = logging.getLogger("qb2api")


def parse_sse_payload(line: str) -> dict | None:
    """Parse a single SSE line into a JSON object."""
    if not line.startswith("data: "):
        return None
    data_str = line[6:].strip()
    if data_str == "[DONE]":
        return None
    try:
        return json.loads(data_str)
    except json.JSONDecodeError:
        return None


def normalize_tool_call_id(tool_call: dict) -> dict:
    """Normalize tool call ID format."""
    raw_id = tool_call.get("id", "")
    if raw_id.startswith("tooluse_"):
        tool_call["id"] = "call_" + raw_id[len("tooluse_"):]
    return tool_call


def inject_tool_call_index(tool_calls: list[dict]) -> list[dict]:
    """Inject missing index field in tool calls."""
    for i, tc in enumerate(tool_calls):
        if "index" not in tc:
            tc["index"] = i
    return tool_calls


class StreamAggregator:
    """Aggregates streaming chunks into a single response."""

    def __init__(self, model: str):
        self.model = model
        self.content_parts: list[str] = []
        self.reasoning_parts: list[str] = []
        self.tool_calls: list[dict] = []
        self.finish_reason = "stop"
        self.usage: dict | None = None

    def process(self, chunk: dict) -> None:
        """Process a single SSE chunk."""
        choices = chunk.get("choices", [])
        if not choices:
            if chunk.get("usage"):
                self.usage = chunk["usage"]
            return

        delta = choices[0].get("delta", {})

        if delta.get("content"):
            self.content_parts.append(delta["content"])
        if delta.get("reasoning_content"):
            self.reasoning_parts.append(delta["reasoning_content"])
        if delta.get("tool_calls"):
            self._merge_tool_calls(delta["tool_calls"])
        if choices[0].get("finish_reason"):
            self.finish_reason = choices[0]["finish_reason"]
        if chunk.get("usage"):
            self.usage = chunk["usage"]

    def _merge_tool_calls(self, new_deltas: list[dict]) -> None:
        """Merge incremental tool call deltas."""
        for tc in new_deltas:
            idx = tc.get("index", 0)
            while len(self.tool_calls) <= idx:
                self.tool_calls.append({
                    "id": f"call_{id(self)}_{len(self.tool_calls)}",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                })

            func = tc.get("function", {})
            if func.get("name"):
                self.tool_calls[idx]["function"]["name"] += func["name"]
            if func.get("arguments"):
                self.tool_calls[idx]["function"]["arguments"] += func["arguments"]
            if tc.get("id"):
                self.tool_calls[idx]["id"] = tc["id"]

    def response(self) -> dict:
        """Build the final response."""
        message = {
            "role": "assistant",
            "content": "".join(self.content_parts) or None,
        }

        if self.reasoning_parts:
            message["reasoning_content"] = "".join(self.reasoning_parts)

        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
            self.finish_reason = "tool_calls"

        return {
            "id": f"chatcmpl-{id(self):x}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": self.finish_reason,
            }],
            "usage": self.usage or {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }


async def collect_stream(stream: AsyncIterator[bytes]) -> dict:
    """Collect an async stream into a single response."""
    aggregator = StreamAggregator(model="unknown")
    async for chunk in stream:
        if chunk.startswith(b"data: [DONE]"):
            break
        try:
            obj = parse_sse_payload(chunk.decode())
            if obj:
                aggregator.process(obj)
        except Exception:
            pass
    return aggregator.response()
