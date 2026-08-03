"""CodeBuddy provider implementation."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx

from ..openai import ChatCompletionRequest, stream_done
from .base import Provider

logger = logging.getLogger("qb2api")

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

CredentialGetter = Callable[[], Awaitable[str]]


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
    REQUEST_KEYS = (
        "temperature", "max_tokens", "max_completion_tokens", "top_p",
        "stop", "presence_penalty", "frequency_penalty", "n",
        "response_format", "seed", "user",
    )

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
            async for chunk in self._response_chunks(resp):
                yield chunk

        yield stream_done()

    async def _response_chunks(self, response: httpx.Response) -> AsyncIterator[bytes]:
        if response.status_code != 200:
            error = (await response.aread()).decode("utf-8", errors="replace")
            raise CodeBuddyError(response.status_code, error[:200])
        async for line in response.aiter_lines():
            chunk, finished = _stream_chunk(line)
            if finished:
                return
            if chunk is not None:
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()

    def _build_body(self, request: ChatCompletionRequest) -> dict:
        """Build upstream request body."""
        body = {
            "model": request.model,
            "messages": _prepared_messages(request),
            "stream": True,
        }
        body.update(_request_values(request, self.REQUEST_KEYS))
        body.update(_tool_values(request))
        body.update(_request_values(request, self.PASSTHROUGH_KEYS))
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


def _stream_chunk(line: str) -> tuple[dict[str, Any] | None, bool]:
    if not line.startswith("data: "):
        return None, False
    data = line[6:]
    if data.strip() == "[DONE]":
        return None, True
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError:
        return None, False
    _normalize_tool_calls(chunk)
    return chunk, False


def _normalize_tool_calls(chunk: dict[str, Any]) -> None:
    choices = chunk.get("choices", [])
    if not choices:
        return
    delta = choices[0].get("delta", {})
    tool_calls = delta.get("tool_calls")
    if not tool_calls:
        return
    from ..sse import inject_tool_call_index, normalize_tool_call_id

    delta["tool_calls"] = [
        normalize_tool_call_id(call)
        for call in inject_tool_call_index(tool_calls)
    ]


def _prepared_messages(request: ChatCompletionRequest) -> list[dict[str, Any]]:
    messages = [message.model_dump(exclude_none=True) for message in request.messages]
    if len(messages) < 2:
        logger.debug("CodeBuddy: injecting default system message (upstream requires >=2 messages)")
        messages.insert(0, {"role": "system", "content": "You are a helpful assistant."})
    return [_scrub_system_message(message) for message in messages]


def _scrub_system_message(message: dict[str, Any]) -> dict[str, Any]:
    if message.get("role") != "system" or "content" not in message:
        return message
    scrubbed = dict(message)
    scrubbed["content"] = scrub_codebuddy_content(message["content"])
    return scrubbed


def _request_values(request: ChatCompletionRequest, keys: tuple[str, ...] | set[str]) -> dict[str, Any]:
    return {
        key: value
        for key in keys
        if (value := getattr(request, key, None)) is not None
    }


def _tool_values(request: ChatCompletionRequest) -> dict[str, Any]:
    if not request.tools:
        return {}
    values: dict[str, Any] = {"tools": [tool.model_dump() for tool in request.tools]}
    if request.tool_choice:
        values["tool_choice"] = request.tool_choice
    return values
