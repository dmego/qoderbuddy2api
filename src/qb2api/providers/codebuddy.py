"""CodeBuddy provider implementation."""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx

from ..openai import ChatCompletionRequest, stream_done
from .base import Provider

logger = logging.getLogger("qb2api")

# Claude Code default identity triggers CodeBuddy content filter.
# Scrub only this outbound phrase; keep the rest of the system prompt.
_CLAUDE_CODE_IDENTITY_RE = re.compile(
    r"You are Claude Code,\s*Anthropic's official CLI for Claude\.?",
    re.IGNORECASE,
)
_SCRUBBED_IDENTITY = "You are a coding CLI assistant."

CredentialGetter = Callable[[], Awaitable[str]]


def scrub_codebuddy_text(text: str) -> str:
    """Remove Claude Code/Anthropic identity phrasing that CodeBuddy rejects."""
    if not text:
        return text
    if "Claude Code" not in text and "official CLI for Claude" not in text:
        return text
    return _CLAUDE_CODE_IDENTITY_RE.sub(_SCRUBBED_IDENTITY, text)


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


class CodeBuddyError(Exception):
    """CodeBuddy upstream error."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"CodeBuddy {status_code}: {message}")


class CodeBuddyProvider(Provider):
    """CodeBuddy API provider.

    Supports static ``token`` (env/manual) and optional async ``credential_getter``
    for dynamic account-backed Bearer resolution per request.
    """

    name = "codebuddy"

    PASSTHROUGH_KEYS = {
        "reasoning_effort", "verbosity", "reasoning_summary",
        "thinking", "max_context_tokens", "context_window",
    }

    def __init__(
        self,
        token: str | None = None,
        endpoint: str = "https://copilot.tencent.com",
        credential_getter: CredentialGetter | None = None,
    ):
        if not token and credential_getter is None:
            raise ValueError("CodeBuddyProvider requires token or credential_getter")
        self.token = token or ""
        self.endpoint = endpoint
        self._credential_getter = credential_getter
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10))

    async def _resolve_token(self) -> str:
        if self._credential_getter is not None:
            token = await self._credential_getter()
            if token:
                return token
        if self.token:
            return self.token
        raise CodeBuddyError(401, "missing bearer credential")

    async def complete(self, request: ChatCompletionRequest) -> dict:
        """Non-streaming completion (aggregated from stream). Raises CodeBuddyError on failure."""
        from ..sse import StreamAggregator

        aggregator = StreamAggregator(model=request.model)
        async for chunk in self.stream(request):
            if chunk.startswith(b"data: [DONE]"):
                break
            try:
                obj = json.loads(chunk[6:].decode().strip())
                if obj:
                    aggregator.process(obj)
            except Exception:
                pass
        request.observe_usage(aggregator.usage)
        return aggregator.response()

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[bytes]:
        """Stream chat completion. Raises CodeBuddyError on upstream failure."""
        url = f"{self.endpoint}/v2/chat/completions"
        body = self._build_body(request)
        headers = await self._build_headers()

        async with self._client.stream("POST", url, json=body, headers=headers) as resp:
            if resp.status_code != 200:
                err = (await resp.aread()).decode("utf-8", errors="replace")
                raise CodeBuddyError(resp.status_code, err[:200])

            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break

                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # Normalize tool calls
                choices = chunk.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    if delta.get("tool_calls"):
                        from ..sse import inject_tool_call_index, normalize_tool_call_id

                        delta["tool_calls"] = inject_tool_call_index(delta["tool_calls"])
                        delta["tool_calls"] = [normalize_tool_call_id(tc) for tc in delta["tool_calls"]]

                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()

        yield stream_done()

    def _build_body(self, request: ChatCompletionRequest) -> dict:
        """Build upstream request body."""
        messages = [msg.model_dump(exclude_none=True) for msg in request.messages]
        # CodeBuddy API requires at least 2 messages; inject a default system prompt when missing
        if len(messages) < 2:
            logger.debug("CodeBuddy: injecting default system message (upstream requires >=2 messages)")
            messages = [{"role": "system", "content": "You are a helpful assistant."}] + messages

        # Scrub Claude Code identity from system messages only (upstream content filter).
        for msg in messages:
            if msg.get("role") == "system" and "content" in msg:
                msg["content"] = scrub_codebuddy_content(msg["content"])

        body = {
            "model": request.model,
            "messages": messages,
            "stream": True,
        }

        for key in (
            "temperature", "max_tokens", "max_completion_tokens", "top_p",
            "stop", "presence_penalty", "frequency_penalty", "n",
            "response_format", "seed", "user",
        ):
            val = getattr(request, key, None)
            if val is not None:
                body[key] = val

        if request.tools:
            body["tools"] = [t.model_dump() for t in request.tools]
            if request.tool_choice:
                body["tool_choice"] = request.tool_choice

        for key in self.PASSTHROUGH_KEYS:
            val = getattr(request, key, None)
            if val is not None:
                body[key] = val

        return body

    async def _build_headers(self) -> dict:
        token = await self._resolve_token()
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "CLI/1.0.8 CodeBuddy/1.0.8",
            "X-Product": "SaaS",
            "X-Domain": "copilot.tencent.com",
            "X-Agent-Intent": "CodeCompletion",
            "Accept": "text/event-stream",
            "X-Machine-Id": str(uuid.uuid4()),
            "X-Request-ID": str(uuid.uuid4()),
        }

    async def close(self) -> None:
        await self._client.aclose()
