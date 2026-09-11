"""CodeBuddy provider implementation."""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx

from ..openai import ChatCompletionRequest, stream_done
from .base import Provider
from .codebuddy_scrub import scrub_codebuddy_content

logger = logging.getLogger("qb2api")

CredentialGetter = Callable[[], Awaitable[str]]


class CodeBuddyError(Exception):
    """CodeBuddy upstream error."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"CodeBuddy {status_code}: {message}")


class CodeBuddyChannelBlockedError(CodeBuddyError):
    """Upstream risk control blocked the calling channel (code 11128).

    Transient by design («Please retry»); surfaced as a distinct type so the
    message is readable instead of raw JSON.
    """


class CodeBuddyQuotaExceededError(CodeBuddyError):
    """Per-account model usage limit exhausted (code 6004).

    The upstream message carries the reset wall clock («将在 … UTC+8 重置»);
    :attr:`model_block_reset_at` exposes it so the provider pool can block
    this account+model pair until then. ``None`` when the message shape is
    unrecognized — callers fall back to the generic cooldown.
    """

    def __init__(self, status_code: int, message: str, reset_at: datetime | None = None):
        super().__init__(status_code, message)
        self.model_block_reset_at = reset_at


_CHANNEL_BLOCKED_CODE = "11128"
_QUOTA_EXCEEDED_CODE = "6004"

_RESET_AT_RE = re.compile(r"将在\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*UTC\+8")
_RESET_TZ = timezone(timedelta(hours=8))


def parse_codebuddy_error(status_code: int, text: str) -> CodeBuddyError:
    """Convert an upstream error body into a typed CodeBuddyError.

    - 11128: the calling channel was flagged by upstream security policy
      (request burst, tool-heavy payload, client fingerprint).
    - 6004: the account exhausted the per-model usage quota; the message
      states the reset time, which the pool uses to auto-disable the
      account+model pair until then.
    Both surface clear messages instead of raw JSON.
    """
    clean = text.strip().replace("\n", " ")
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return CodeBuddyError(status_code, clean[:200])
    code = str(payload.get("code", ""))
    if code == _CHANNEL_BLOCKED_CODE:
        message = payload.get("msg") or clean
        display = payload.get("displayMsg")
        if isinstance(display, dict) and display.get("en"):
            detail = f"{message}; {display['en']}"
        else:
            detail = message
        return CodeBuddyChannelBlockedError(
            status_code, f"{detail} — upstream security policy; back off and retry later"
        )
    if code == _QUOTA_EXCEEDED_CODE:
        message = payload.get("msg") or clean
        return CodeBuddyQuotaExceededError(status_code, message, _parse_reset_at(message))
    return CodeBuddyError(status_code, clean[:200])


def _parse_reset_at(message: str) -> datetime | None:
    match = _RESET_AT_RE.search(message)
    if match is None:
        return None
    try:
        naive = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=_RESET_TZ).astimezone(UTC)


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
        default_reasoning_effort: str | None = None,
    ):
        if not token and credential_getter is None:
            raise ValueError("CodeBuddyProvider requires token or credential_getter")
        self.token = token or ""
        self.endpoint = endpoint
        self._credential_getter = credential_getter
        self.default_reasoning_effort = (default_reasoning_effort or "").strip().lower() or None
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10), trust_env=False)

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
            raise parse_codebuddy_error(response.status_code, error)
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
        if self.default_reasoning_effort and "reasoning_effort" not in body:
            # WorkBuddy models only emit reasoning_content when reasoning_effort
            # is set; inject a default so supported models actually think.
            body["reasoning_effort"] = self.default_reasoning_effort
            request.record_effective_reasoning_effort(self.default_reasoning_effort)
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
    return [_normalize_outbound_message(message) for message in messages]


def _normalize_outbound_message(message: dict[str, Any]) -> dict[str, Any]:
    """Fold OpenAI's ``developer`` role into ``system`` before sending upstream.

    CodeBuddy answers any request carrying a ``developer`` message with 400
    code 11128 ("Illegal API invocation from an unapproved channel"), while the
    identical text on the ``system`` role passes. Newer OpenAI-compatible
    harnesses (DeepSeek Harness, Codex-style clients) deliver their harness
    prompt on that role, so without this fold every such request is rejected.
    """
    if isinstance(message, dict) and message.get("role") == "developer":
        message = {**message, "role": "system"}
    return _scrub_system_message(message)


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
