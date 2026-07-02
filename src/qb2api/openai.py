"""OpenAI-compatible data models."""

import time as _time
import uuid as _uuid
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict


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
