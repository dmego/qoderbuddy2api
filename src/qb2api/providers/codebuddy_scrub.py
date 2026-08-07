"""Outbound system-prompt scrubbing for the CodeBuddy content filter."""

from __future__ import annotations

from typing import Any

# Claude Code's system prompt triggers the CodeBuddy content filter.
# Replace the whole outbound system message with a neutral one when it
# contains Claude/Anthropic identity phrasing (any prompt variant).
_CLAUDE_SYSTEM_SENTINELS = (
    "You are Claude Code",
    "You are a Claude agent",
    "Anthropic's official CLI for Claude",
    "Claude Agent SDK",
)
_NEUTRAL_SYSTEM = "You are a helpful assistant."


def scrub_codebuddy_text(text: str) -> str:
    """Replace Claude/Anthropic system prompts CodeBuddy rejects."""
    if not text:
        return text
    if not any(sentinel in text for sentinel in _CLAUDE_SYSTEM_SENTINELS):
        return text
    return _NEUTRAL_SYSTEM


def scrub_codebuddy_content(content: Any) -> Any:
    """Scrub string or multimodal text blocks in a message content field."""
    if isinstance(content, str):
        return scrub_codebuddy_text(content)
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                b = dict(block)
                b["text"] = scrub_codebuddy_text(b["text"])
                out.append(b)
            else:
                out.append(block)
        return out
    return content
