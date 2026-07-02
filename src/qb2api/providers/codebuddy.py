"""CodeBuddy provider implementation."""

import json
import uuid
import logging
from typing import AsyncIterator

import httpx

from .base import Provider
from ..openai import ChatCompletionRequest, stream_done

logger = logging.getLogger("qb2api")


class CodeBuddyError(Exception):
    """CodeBuddy upstream error."""
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"CodeBuddy {status_code}: {message}")


class CodeBuddyProvider(Provider):
    """CodeBuddy API provider."""

    name = "codebuddy"

    PASSTHROUGH_KEYS = {
        "reasoning_effort", "verbosity", "reasoning_summary",
        "thinking", "max_context_tokens", "context_window",
    }

    def __init__(self, token: str, endpoint: str = "https://copilot.tencent.com"):
        self.token = token
        self.endpoint = endpoint
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10))

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
        return aggregator.response()

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[bytes]:
        """Stream chat completion. Raises CodeBuddyError on upstream failure."""
        url = f"{self.endpoint}/v2/chat/completions"
        body = self._build_body(request)
        headers = self._build_headers()

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
                        from ..sse import normalize_tool_call_id, inject_tool_call_index
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

        body = {
            "model": request.model,
            "messages": messages,
            "stream": True,
        }

        for key in ("temperature", "max_tokens", "max_completion_tokens", "top_p",
                     "stop", "presence_penalty", "frequency_penalty", "n",
                     "response_format", "seed", "user"):
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

    def _build_headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
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
