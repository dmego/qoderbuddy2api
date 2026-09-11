"""WorkBuddy international provider (www.workbuddy.ai).

Same SSE chat contract as the domestic pool but on its own origin, with one
extra upstream requirement: ``messages[0]`` must be a ``system`` message —
otherwise the gateway answers ``code=11128 "first message is not system
prompt"``. The international deployment also has no sign-in/growth centre, so
it is chat-only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..openai import ChatCompletionRequest, stream_done
from .base import Provider
from .codebuddy import (
    CodeBuddyError,
    CredentialGetter,
    _request_values,
    _stream_chunk,
    _tool_values,
)

INTL_FALLBACK_SYSTEM_PROMPT = "You are a helpful assistant."


class WorkBuddyIntlProvider(Provider):
    """Chat provider bound to the international WorkBuddy deployment."""

    name = "workbuddy_intl"

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
        *,
        token: str | None = None,
        endpoint: str = "https://www.workbuddy.ai",
        credential_getter: CredentialGetter | None = None,
        default_reasoning_effort: str | None = None,
    ):
        if not token and credential_getter is None:
            raise ValueError("WorkBuddyIntlProvider requires token or credential_getter")
        self.token = token or ""
        self.endpoint = endpoint.rstrip("/")
        self._credential_getter = credential_getter
        self.default_reasoning_effort = (default_reasoning_effort or "").strip().lower() or None
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(300, connect=10),
            trust_env=False,
        )

    async def _resolve_token(self) -> str:
        if self._credential_getter is not None:
            token = await self._credential_getter()
            if token:
                return token
        if self.token:
            return self.token
        raise CodeBuddyError(401, "missing bearer credential")

    async def complete(self, request: ChatCompletionRequest) -> dict:
        """Non-streaming completion aggregated from the upstream SSE stream."""
        import json

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
        url = f"{self.endpoint}/v2/chat/completions"
        body = self._build_body(request)
        headers = await self._build_headers()
        async with self._client.stream("POST", url, json=body, headers=headers) as response:
            async for chunk in self._response_chunks(response):
                yield chunk
        yield stream_done()

    async def _response_chunks(self, response: Any) -> AsyncIterator[bytes]:
        import json

        if response.status_code != 200:
            from .codebuddy import parse_codebuddy_error

            error = (await response.aread()).decode("utf-8", errors="replace")
            raise parse_codebuddy_error(response.status_code, error)
        async for line in response.aiter_lines():
            chunk, finished = _stream_chunk(line)
            if finished:
                return
            if chunk is not None:
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()

    def _build_body(self, request: ChatCompletionRequest) -> dict:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": intl_messages(request),
            "stream": True,
        }
        body.update(_request_values(request, self.REQUEST_KEYS))
        body.update(_tool_values(request))
        body.update(_request_values(request, self.PASSTHROUGH_KEYS))
        if self.default_reasoning_effort and "reasoning_effort" not in body:
            body["reasoning_effort"] = self.default_reasoning_effort
            request.record_effective_reasoning_effort(self.default_reasoning_effort)
        return body

    async def _build_headers(self) -> dict:
        import uuid

        token = await self._resolve_token()
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
            "User-Agent": "CLI/1.0.8 CodeBuddy/1.0.8",
            "X-Product": "SaaS",
            "X-Domain": "www.workbuddy.ai",
            "X-Machine-Id": str(uuid.uuid4()),
            "X-Request-ID": str(uuid.uuid4()),
        }

    async def close(self) -> None:
        await self._client.aclose()


def intl_messages(request: ChatCompletionRequest) -> list[dict[str, Any]]:
    """Build outbound messages with a guaranteed leading ``system`` message.

    The international gateway rejects any request whose first message is not a
    ``system`` message, and rejects the OpenAI ``developer`` role outright, so
    the role is folded into ``system`` and a placeholder is prepended when the
    client's first message is a user/assistant/tool turn.
    """
    from .codebuddy import _normalize_outbound_message

    messages = [_normalize_outbound_message(message.model_dump(exclude_none=True))
                for message in request.messages]
    if not messages or messages[0].get("role") != "system":
        messages.insert(0, {"role": "system", "content": INTL_FALLBACK_SYSTEM_PROMPT})
    return messages


__all__ = [
    "INTL_FALLBACK_SYSTEM_PROMPT",
    "WorkBuddyIntlProvider",
    "intl_messages",
]
