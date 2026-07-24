"""Anthropic Messages API compatibility helpers."""

import json
import uuid
from typing import Any

from .anthropic_content import anthropic_message_to_openai as _anthropic_message_to_openai
from .anthropic_content import content_to_text as _content_to_text


def anthropic_to_openai(body: dict[str, Any]) -> dict[str, Any]:
    """Convert an Anthropic Messages request body into an OpenAI chat request."""
    request = _request_base(body)
    request.update(_optional_request_values(body))
    return request


def _request_base(body: dict[str, Any]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    system_text = _content_to_text(body.get("system"))
    if system_text:
        messages.append({"role": "system", "content": system_text})
    for message in body.get("messages", []):
        messages.extend(_anthropic_message_to_openai(message))
    return {
        "model": body.get("model"),
        "messages": messages,
        "stream": bool(body.get("stream", False)),
    }


def _optional_request_values(body: dict[str, Any]) -> dict[str, Any]:
    request: dict[str, Any] = {}
    for values in (
        _completion_options(body),
        _metadata_options(body),
        _tool_options(body),
        _context_options(body),
    ):
        request.update(values)
    return request


def _completion_options(body: dict[str, Any]) -> dict[str, Any]:
    request: dict[str, Any] = {}
    if body.get("max_tokens") is not None:
        request["max_tokens"] = body["max_tokens"]
    for source, target in (
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("stop_sequences", "stop"),
    ):
        if body.get(source) is not None:
            request[target] = body[source]
    return request


def _metadata_options(body: dict[str, Any]) -> dict[str, Any]:
    metadata = body.get("metadata") or {}
    if isinstance(metadata, dict) and metadata.get("user_id"):
        return {"user": metadata["user_id"]}
    return {}


def _tool_options(body: dict[str, Any]) -> dict[str, Any]:
    request: dict[str, Any] = {}
    tools = [_anthropic_tool_to_openai(tool) for tool in body.get("tools") or []]
    if tools:
        request["tools"] = tools
    tool_choice = _anthropic_tool_choice_to_openai(body.get("tool_choice"))
    if tool_choice is not None:
        request["tool_choice"] = tool_choice
    return request


def _context_options(body: dict[str, Any]) -> dict[str, Any]:
    request: dict[str, Any] = {}
    for key in ("reasoning_effort", "context_window", "max_context_tokens"):
        if body.get(key) is not None:
            request[key] = body[key]
    return request


def openai_to_anthropic(response: dict[str, Any], model: str) -> dict[str, Any]:
    """Convert an OpenAI chat completion response into Anthropic message shape."""
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return _anthropic_response(
        response=response,
        choice=choice,
        message=message,
        model=model,
    )


def _anthropic_response(
    *,
    response: dict[str, Any],
    choice: dict[str, Any],
    message: dict[str, Any],
    model: str,
) -> dict[str, Any]:
    return {
        "id": _message_id(response.get("id")),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": _response_content(message),
        "stop_reason": map_finish_reason(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": _response_usage(response),
    }


def _response_content(message: dict[str, Any]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    text = message.get("content")
    if text:
        content.append({"type": "text", "text": str(text)})
    for tool_call in message.get("tool_calls") or []:
        content.append(_tool_use_content(tool_call))
    return content


def _tool_use_content(tool_call: dict[str, Any]) -> dict[str, Any]:
    function = tool_call.get("function") or {}
    return {
        "type": "tool_use",
        "id": tool_call.get("id") or f"toolu_{uuid.uuid4().hex[:16]}",
        "name": function.get("name") or "",
        "input": _parse_tool_input(function.get("arguments")),
    }


def _response_usage(response: dict[str, Any]) -> dict[str, int]:
    usage = response.get("usage") or {}
    return {
        "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
    }


def _anthropic_tool_to_openai(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.get("name") or "",
            "description": tool.get("description") or "",
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
        },
    }


def _anthropic_tool_choice_to_openai(tool_choice: Any) -> Any:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        return tool_choice
    if not isinstance(tool_choice, dict):
        return tool_choice
    choice_type = tool_choice.get("type")
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "none":
        return "none"
    if choice_type == "tool":
        return {"type": "function", "function": {"name": tool_choice.get("name") or ""}}
    return tool_choice


def _parse_tool_input(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if not arguments:
        return {}
    try:
        parsed = json.loads(str(arguments))
    except json.JSONDecodeError:
        return {"_raw": str(arguments)}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _message_id(openai_id: Any) -> str:
    raw = str(openai_id or "")
    if raw.startswith("msg_"):
        return raw
    suffix = raw.removeprefix("chatcmpl-") or uuid.uuid4().hex[:24]
    return f"msg_{suffix}"


def map_finish_reason(reason: Any) -> str | None:
    if reason is None:
        return None
    return {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "stop_sequence",
    }.get(str(reason), str(reason))
