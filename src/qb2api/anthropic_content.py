"""Anthropic content-block conversion helpers."""

from __future__ import annotations

import json
import uuid
from typing import Any


def anthropic_message_to_openai(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert one Anthropic message while preserving block ordering."""
    role = message.get("role", "user")
    content = message.get("content", "")
    if role == "assistant" and isinstance(content, list):
        return [_assistant_message(content)]
    if isinstance(content, str):
        return [{"role": role, "content": content}]
    if not isinstance(content, list):
        return [{"role": role, "content": content_to_text(content)}]
    return _content_blocks_to_messages(role, content)


def _assistant_message(content: list[Any]) -> dict[str, Any]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in content:
        _collect_assistant_block(block, text_parts=text_parts, tool_calls=tool_calls)
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "\n".join(part for part in text_parts if part),
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _collect_assistant_block(
    block: Any,
    *,
    text_parts: list[str],
    tool_calls: list[dict[str, Any]],
) -> None:
    if not isinstance(block, dict):
        text_parts.append(str(block))
        return
    if block.get("type") == "text":
        text_parts.append(str(block.get("text", "")))
        return
    if block.get("type") == "tool_use":
        tool_calls.append(_tool_call(block))


def _tool_call(block: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": block.get("id") or f"toolu_{uuid.uuid4().hex[:16]}",
        "type": "function",
        "function": {
            "name": block.get("name") or "",
            "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
        },
    }


def _content_blocks_to_messages(role: str, content: list[Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for block in content:
        _append_content_block(block, messages=messages, role=role, text_parts=text_parts)
    _append_text_message(messages, role, text_parts)
    return messages or [{"role": role, "content": ""}]


def _append_content_block(
    block: Any,
    *,
    messages: list[dict[str, Any]],
    role: str,
    text_parts: list[str],
) -> None:
    if not isinstance(block, dict):
        text_parts.append(str(block))
        return
    block_type = block.get("type")
    if block_type == "text":
        text_parts.append(str(block.get("text", "")))
        return
    if block_type == "tool_result":
        _append_text_message(messages, role, text_parts)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": block.get("tool_use_id") or block.get("id") or "",
                "content": content_to_text(block.get("content")),
            }
        )
        return
    if block_type == "image":
        text_parts.append("[image]")
        return
    text_parts.append(content_to_text(block))


def _append_text_message(
    messages: list[dict[str, Any]],
    role: str,
    text_parts: list[str],
) -> None:
    if text_parts:
        messages.append({"role": role, "content": "\n".join(part for part in text_parts if part)})
        text_parts.clear()


def content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return _content_items_to_text(content)
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text", ""))
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def _content_items_to_text(content: list[Any]) -> str:
    parts = [_content_item_to_text(item) for item in content]
    return "\n".join(part for part in parts if part)


def _content_item_to_text(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    item_type = item.get("type")
    if item_type == "text":
        return str(item.get("text", ""))
    if item_type == "tool_result":
        return content_to_text(item.get("content"))
    if item_type == "image":
        return "[image]"
    return json.dumps(item, ensure_ascii=False)
