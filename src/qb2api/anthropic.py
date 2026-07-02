"""Anthropic Messages API compatibility helpers."""

import json
import uuid
from typing import Any


def anthropic_to_openai(body: dict[str, Any]) -> dict[str, Any]:
    """Convert an Anthropic Messages request body into an OpenAI chat request."""
    messages: list[dict[str, Any]] = []
    system_text = _content_to_text(body.get("system"))
    if system_text:
        messages.append({"role": "system", "content": system_text})

    for message in body.get("messages", []):
        messages.extend(_anthropic_message_to_openai(message))

    request: dict[str, Any] = {
        "model": body.get("model"),
        "messages": messages,
        "stream": bool(body.get("stream", False)),
    }

    if body.get("max_tokens") is not None:
        request["max_tokens"] = body["max_tokens"]
    for source, target in (
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("stop_sequences", "stop"),
    ):
        if body.get(source) is not None:
            request[target] = body[source]

    metadata = body.get("metadata") or {}
    if isinstance(metadata, dict) and metadata.get("user_id"):
        request["user"] = metadata["user_id"]

    tools = [_anthropic_tool_to_openai(tool) for tool in body.get("tools") or []]
    if tools:
        request["tools"] = tools

    tool_choice = _anthropic_tool_choice_to_openai(body.get("tool_choice"))
    if tool_choice is not None:
        request["tool_choice"] = tool_choice

    for key in ("reasoning_effort", "context_window", "max_context_tokens"):
        if body.get(key) is not None:
            request[key] = body[key]

    return request


def openai_to_anthropic(response: dict[str, Any], model: str) -> dict[str, Any]:
    """Convert an OpenAI chat completion response into Anthropic message shape."""
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content: list[dict[str, Any]] = []

    text = message.get("content")
    if text:
        content.append({"type": "text", "text": str(text)})

    for tool_call in message.get("tool_calls") or []:
        function = tool_call.get("function") or {}
        content.append(
            {
                "type": "tool_use",
                "id": tool_call.get("id") or f"toolu_{uuid.uuid4().hex[:16]}",
                "name": function.get("name") or "",
                "input": _parse_tool_input(function.get("arguments")),
            }
        )

    usage = response.get("usage") or {}
    return {
        "id": _message_id(response.get("id")),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": map_finish_reason(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        },
    }


def _anthropic_message_to_openai(message: dict[str, Any]) -> list[dict[str, Any]]:
    role = message.get("role", "user")
    content = message.get("content", "")

    if role == "assistant" and isinstance(content, list):
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                text_parts.append(str(block))
                continue
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(str(block.get("text", "")))
            elif block_type == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id") or f"toolu_{uuid.uuid4().hex[:16]}",
                        "type": "function",
                        "function": {
                            "name": block.get("name") or "",
                            "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                        },
                    }
                )
        out: dict[str, Any] = {"role": "assistant", "content": "\n".join(p for p in text_parts if p)}
        if tool_calls:
            out["tool_calls"] = tool_calls
        return [out]

    if isinstance(content, str):
        return [{"role": role, "content": content}]

    if not isinstance(content, list):
        return [{"role": role, "content": _content_to_text(content)}]

    out: list[dict[str, Any]] = []
    text_parts: list[str] = []

    def flush_text() -> None:
        if text_parts:
            out.append({"role": role, "content": "\n".join(part for part in text_parts if part)})
            text_parts.clear()

    for block in content:
        if not isinstance(block, dict):
            text_parts.append(str(block))
            continue
        block_type = block.get("type")
        if block_type == "text":
            text_parts.append(str(block.get("text", "")))
        elif block_type == "tool_result":
            flush_text()
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id") or block.get("id") or "",
                    "content": _content_to_text(block.get("content")),
                }
            )
        elif block_type == "image":
            text_parts.append("[image]")
        else:
            text_parts.append(_content_to_text(block))
    flush_text()
    return out or [{"role": role, "content": ""}]


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


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type == "text":
                    parts.append(str(item.get("text", "")))
                elif item_type == "tool_result":
                    parts.append(_content_to_text(item.get("content")))
                elif item_type == "image":
                    parts.append("[image]")
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text", ""))
        return json.dumps(content, ensure_ascii=False)
    return str(content)


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
