"""Qoder COSY model mapping, request payloads, and SSE delta parsing."""

from __future__ import annotations

import base64
import json
import re
import uuid
from typing import Any

from ..openai import ChatCompletionRequest, ChatMessage
from ..sse import inject_tool_call_index, normalize_tool_call_id

EFFORT_SUFFIX_RE = re.compile(r"^(.*)-effort-(low|medium|high|max)$")

QODER_CLI_MODEL_KEYS = {
    "auto": "auto",
    "Auto": "auto",
    "Qwen3.8-Max-Preview": "qmodel_preview",
    "Qwen3.7-Max": "qmodel_latest",
    "Qwen3.7-Plus": "qmodel",
    "Qwen3.6-Flash": "q36fmodel",
    "DeepSeek-V4-Pro": "dmodel",
    "DeepSeek-V4-Flash": "dfmodel",
    "GLM-5.2": "gm51model",
    "Kimi-K2.7-Code": "kmodel",
    "MiniMax-M2.7": "mmodel",
}

_STANDARD_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_QODER_ALPHABET = "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!"
_QODER_TRANSLATION = str.maketrans(
    _STANDARD_ALPHABET + "=",
    _QODER_ALPHABET + "$",
)


_runtime_model_keys: dict[str, str] = {}


def set_runtime_model_keys(mapping: dict[str, str]) -> None:
    """Replace the runtime model-key mapping (from synced model metadata)."""
    _runtime_model_keys.clear()
    _runtime_model_keys.update(mapping)


def clear_runtime_model_keys() -> None:
    """Drop the runtime model-key mapping, restoring static-table behavior."""
    _runtime_model_keys.clear()


def qoder_model_key(model: str) -> str:
    """Map the public CLI display model to the COSY internal key."""
    if model in _runtime_model_keys:
        return _runtime_model_keys[model]
    return QODER_CLI_MODEL_KEYS.get(model, model)


def qoder_encode(data: bytes) -> str:
    """Encode request bytes with the Qoder rotated/custom base64 alphabet."""
    encoded = base64.b64encode(data).decode("ascii")
    third = len(encoded) // 3
    rotated = encoded[-third:] + encoded[third:-third] + encoded[:third]
    return rotated.translate(_QODER_TRANSLATION)


def build_qoder_payload(request: ChatCompletionRequest, model: str) -> dict[str, Any]:
    """Build the Qoder COSY body while preserving OpenAI message roles."""
    upstream_model = qoder_model_key(model)
    messages, user_text = _qoder_messages(request)
    request_id = str(uuid.uuid4())
    payload: dict[str, Any] = {
        "request_id": request_id,
        "chat_record_id": request_id,
        "request_set_id": str(uuid.uuid4()),
        "session_id": str(uuid.uuid4()),
        "stream": True,
        "model_config": {"key": upstream_model, "source": "system"},
        "chat_context": _chat_context(user_text),
        "messages": messages,
        "source": 1,
        "version": "3",
    }
    if request.tools:
        payload["tools"] = [tool.model_dump() for tool in request.tools]
        if request.tool_choice:
            payload["tool_choice"] = request.tool_choice
    return payload


def _qoder_messages(request: ChatCompletionRequest) -> tuple[list[dict[str, Any]], str]:
    user_text = ""
    messages: list[dict[str, Any]] = []
    for message in request.messages:
        content = _message_text(message.content)
        entry, current_user_text = _qoder_message(message, content)
        messages.append(entry)
        if current_user_text is not None:
            user_text = current_user_text
    return messages, user_text


def _qoder_message(message: ChatMessage, content: str) -> tuple[dict[str, Any], str | None]:
    entry = _message_entry(message.role, content)
    if message.role == "user":
        entry["content"] = ""
        entry["contents"] = [{"type": "text", "text": content}]
        return entry, content
    if message.role == "assistant" and message.tool_calls:
        entry["tool_calls"] = message.tool_calls
    if message.role == "tool":
        entry["tool_call_id"] = message.tool_call_id
    return entry, None


def _chat_context(user_text: str) -> dict[str, Any]:
    return {
        "text": {"text": user_text},
        "extra": {"originalContent": {"text": user_text}},
    }


def parse_qoder_sse_data(data: str) -> tuple[dict[str, Any], str | None] | None:
    """Parse one upstream SSE data value into an OpenAI delta and finish reason."""
    if not data or data == "[DONE]":
        return None
    inner = _decode_sse_body(data)
    if inner is None:
        return None
    choice = _first_choice(inner)
    if choice is None:
        return None
    return _choice_delta(choice)


def _decode_sse_body(data: str) -> dict[str, Any] | None:
    try:
        outer = json.loads(data, strict=False)
        body = outer.get("body")
        inner = json.loads(body, strict=False) if isinstance(body, str) else outer
    except (AttributeError, json.JSONDecodeError, TypeError):
        return None
    return inner if isinstance(inner, dict) else None


def _first_choice(body: dict[str, Any]) -> dict[str, Any] | None:
    choices = body.get("choices", [])
    if not choices or not isinstance(choices[0], dict):
        return None
    return choices[0]


def _choice_delta(choice: dict[str, Any]) -> tuple[dict[str, Any], str | None] | None:
    delta = choice.get("delta") or {}
    if not isinstance(delta, dict):
        return None
    output = _normalized_delta(delta)
    finish = choice.get("finish_reason")
    return output, str(finish) if finish is not None else None


def _message_text(content: Any) -> str:
    if not isinstance(content, list):
        return str(content or "")
    parts = [
        text
        for block in content
        if (text := _message_block_text(block)) is not None
    ]
    return " ".join(parts)


def _message_block_text(block: Any) -> str | None:
    if not isinstance(block, dict):
        return str(block)
    if block.get("type") == "text":
        return str(block.get("text", ""))
    if block.get("type") == "image_url":
        return "[image]"
    return None


def _message_entry(role: str, content: str) -> dict[str, Any]:
    return {
        "role": role,
        "content": content,
        "response_meta": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        },
    }


def _normalized_delta(delta: dict[str, Any]) -> dict[str, Any]:
    output = {
        key: delta[key]
        for key in ("role", "content", "reasoning_content")
        if delta.get(key)
    }
    tool_calls = delta.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        indexed = inject_tool_call_index(tool_calls)
        output["tool_calls"] = [normalize_tool_call_id(call) for call in indexed]
    return output
