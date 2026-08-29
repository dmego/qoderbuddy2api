"""OpenAI-compatible data models."""

import json
import time as _time
import uuid as _uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, PrivateAttr


class ModelInfo(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = 0
    owned_by: str = "qoderbuddy2api"


class ModelListResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelInfo]


class ChatMessage(BaseModel):
    role: str
    content: Any = None
    name: str | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None


class ToolDefinition(BaseModel):
    type: Literal["function"] = "function"
    function: dict


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage]
    stream: bool = False
    tools: list[ToolDefinition] | None = None
    tool_choice: Any = None
    temperature: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    top_p: float | None = None
    stop: str | list[str] | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    n: int | None = None
    response_format: dict | None = None
    seed: int | None = None
    user: str | None = None

    _selected_provider: str | None = PrivateAttr(default=None)
    _selected_account_id: str | None = PrivateAttr(default=None)
    _stream_committed: bool = PrivateAttr(default=False)
    _input_tokens: int | None = PrivateAttr(default=None)
    _output_tokens: int | None = PrivateAttr(default=None)
    _effective_reasoning_effort: str | None = PrivateAttr(default=None)

    def record_provider(self, provider: str) -> None:
        self._selected_provider = provider

    def record_effective_reasoning_effort(self, effort: str) -> None:
        """Record the effort actually applied to the upstream request."""
        self._effective_reasoning_effort = effort

    def record_slot(self, slot_key: str, *, committed: bool = False) -> None:
        self._selected_account_id = slot_key.split(":", 1)[-1]
        self._stream_committed = self._stream_committed or committed

    def observe_usage(self, usage: dict[str, Any] | None) -> None:
        if not isinstance(usage, dict):
            return
        self._input_tokens = _token_value(usage, "prompt_tokens", "input_tokens")
        self._output_tokens = _token_value(usage, "completion_tokens", "output_tokens")

    def observe_stream_chunk(self, chunk: bytes) -> None:
        text = chunk.decode("utf-8", errors="ignore")
        for line in text.splitlines():
            if not line.startswith("data: ") or line[6:].strip() == "[DONE]":
                continue
            try:
                body = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            self.observe_usage(body.get("usage") if isinstance(body, dict) else None)

    @property
    def telemetry(self) -> dict[str, Any]:
        return {
            "provider": self._selected_provider,
            "account_id": self._selected_account_id,
            "stream_committed": self._stream_committed,
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "reasoning_effort": self._effective_reasoning_effort
            or getattr(self, "reasoning_effort", None),
        }


def _token_value(usage: dict[str, Any], *names: str) -> int | None:
    for name in names:
        value = usage.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class StreamChoice(BaseModel):
    index: int = 0
    delta: dict
    finish_reason: str | None = None


class StreamChunk(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[StreamChoice]


def stream_done() -> bytes:
    return b"data: [DONE]\n\n"


def stream_chunk(
    model: str,
    delta: dict,
    finish_reason: str | None = None,
    *,
    request_id: str | None = None,
    created: int | None = None,
) -> bytes:
    """Create a streaming chunk. Uses provided request_id/created or generates new ones."""
    chunk = StreamChunk(
        id=request_id or f"chatcmpl-{_uuid.uuid4().hex[:12]}",
        created=created or int(_time.time()),
        model=model,
        choices=[StreamChoice(delta=delta, finish_reason=finish_reason)],
    )
    return f"data: {chunk.model_dump_json()}\n\n".encode()


def error_sse(message: str) -> bytes:
    """Create an error SSE event (OpenAI-compatible error object, NOT a fake assistant message)."""
    import json
    error_event = {"error": {"message": message, "type": "upstream_error"}}
    return f"data: {json.dumps(error_event)}\n\n".encode()
