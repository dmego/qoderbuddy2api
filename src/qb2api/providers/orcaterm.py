"""OrcaTerm provider (Tencent Cloud OrcaTerm desktop agent, lightai backend).

Upstream contract (reverse-engineered and verified 2026-09-11):

- Chat is an *agent* endpoint, not a plain completion API:
  ``POST {endpoint}/assistant/chat?name=orcaterm&mode=0&csrfCode=`` returns an
  SSE stream whose ``content`` fragments concatenate into an agent JSON object
  ``{"action", "thinking", "taskCompletion"}``.
- Every conversation must be registered first:
  ``POST {endpoint}/assistant/conversation?name=orcaterm&mode=0&csrfCode=``.
- Auth is a desktop OAuth JWT (``X-Product: orcaterm`` + ``Authorization:
  Bearer <token>``); the credential lives in the OrcaTerm desktop store
  (``data.bin`` → ``oauth_access_token``) and expires after ~2h.
- Model ids are ``<Provider>/<model>`` (e.g. ``TokenHub/glm-5.3``,
  ``Hunyuan3/hy4-preview``).
- The assistant carries terminal-oriented MCP tools server-side; the request
  disables them so the answer is plain text.
"""

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

CredentialGetter = Callable[[], Awaitable[str]]

ORCATERM_ASSISTANT = "orcaterm"
ORCATERM_MODE = 0

# The agent replies with a fenced JSON object; strip the fence before parsing.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


class OrcaTermError(Exception):
    """OrcaTerm upstream error."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"OrcaTerm {status_code}: {message}")


class OrcaTermQuotaError(OrcaTermError):
    """Upstream rate limit (``chat-rpm``) or quota exhaustion."""


class OrcaTermAuthError(OrcaTermError):
    """Credential missing, expired, or rejected (``NEED_UIN_AND_SKEY``)."""


class OrcaTermProvider(Provider):
    """Chat provider bound to the OrcaTerm lightai agent backend."""

    name = "orcaterm"

    # The agent endpoint is OpenAI-shaped for transport but not for semantics:
    # sampling knobs are accepted and forwarded, message history travels as
    # ``historyMessages`` inside the input object.
    REQUEST_KEYS = (
        "temperature",
        "top_p",
        "max_tokens",
        "stop",
        "presence_penalty",
        "frequency_penalty",
    )

    def __init__(
        self,
        *,
        token: str | None = None,
        endpoint: str = "https://lightai.cloud.tencent.com",
        user_id: str = "",
        credential_getter: CredentialGetter | None = None,
        timeout: int = 300,
    ):
        if not token and credential_getter is None:
            raise ValueError("OrcaTermProvider requires token or credential_getter")
        self.token = token or ""
        self.endpoint = endpoint.rstrip("/")
        self.user_id = user_id
        self._credential_getter = credential_getter
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10),
            trust_env=False,
        )
        # Registered conversations are reusable; one per provider instance is
        # enough because history travels in the request body.
        self._conversation_id: str | None = None

    # ---------------------------------------------------------------- auth

    async def _resolve_token(self) -> str:
        if self._credential_getter is not None:
            token = await self._credential_getter()
            if token:
                return token
        if self.token:
            return self.token
        raise OrcaTermAuthError(401, "missing OrcaTerm credential")

    async def _headers(self) -> dict[str, str]:
        token = await self._resolve_token()
        return {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {token}",
            "X-Product": "orcaterm",
            "Origin": "https://orcaterm.cloud.tencent.com",
            "Referer": "https://orcaterm.cloud.tencent.com/",
            "User-Agent": "OrcaTerm/1.5.2",
        }

    def _query(self) -> str:
        return f"name={ORCATERM_ASSISTANT}&mode={ORCATERM_MODE}&csrfCode="

    # --------------------------------------------------------- conversation

    def _default_conversation_id(self) -> str:
        seed = self.user_id or uuid.uuid4().hex
        return f"cid-{seed}-{ORCATERM_ASSISTANT}"

    async def _ensure_conversation(self) -> str:
        """Register (once) the conversation this provider streams into."""
        if self._conversation_id:
            return self._conversation_id
        import time

        conversation_id = self._default_conversation_id()
        url = f"{self.endpoint}/assistant/conversation?{self._query()}"
        payload = {
            "id": conversation_id,
            "title": "api",
            "tags": [ORCATERM_ASSISTANT],
            "ts": int(time.time() * 1000),
            "customTitle": "",
            "aiTitle": "",
        }
        headers = await self._headers()
        response = await self._client.post(url, json=payload, headers=headers)
        body = (await response.aread()).decode("utf-8", errors="replace")
        if response.status_code != 200:
            raise _parse_error(response.status_code, body)
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError:
            decoded = {}
        code = decoded.get("code")
        # ``code`` 0 is success; an already-registered id answers with an error
        # that is safe to ignore because the conversation is usable either way.
        if code not in (0, None) and "exist" not in str(decoded.get("message", "")):
            logger.debug("orcaterm conversation register: %s", str(decoded)[:200])
        self._conversation_id = conversation_id
        return conversation_id

    # ------------------------------------------------------------- payload

    def _build_body(
        self,
        request: ChatCompletionRequest,
        conversation_id: str,
    ) -> dict[str, Any]:
        setting: dict[str, Any] = {
            "model": request.model,
            "language": "zh",
            "mcpServers": [],
            "uiServers": [],
            "tools": [],
            "approvedTools": [],
            "approvedMCPTools": [],
            "enableAutoSubtaskExecution": False,
            "selectedMCPTools": {},
            "selectedUITools": {},
        }
        body: dict[str, Any] = {
            "conversationId": conversation_id,
            "user": {"id": self.user_id or "", "setting": setting},
            "input": {
                "type": "start",
                "input": _last_user_text(request),
                "id": str(uuid.uuid4()),
                "emphasisPrompt": "",
                "files": [],
                "historyMessages": _history_messages(request),
            },
            "stream": True,
        }
        for key in self.REQUEST_KEYS:
            value = getattr(request, key, None)
            if value is not None:
                setting.setdefault("sampling", {})[key] = value
        return body

    # ------------------------------------------------------------ provider

    async def complete(self, request: ChatCompletionRequest) -> dict:
        """Non-streaming completion aggregated from the upstream agent stream."""
        import time

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        async for event in self._agent_events(request):
            if event.get("content"):
                text_parts.append(event["content"])
            if event.get("reasoningContent"):
                reasoning_parts.append(event["reasoningContent"])
        answer, thinking = _split_agent_payload("".join(text_parts))
        if thinking:
            reasoning_parts.insert(0, thinking)
        content = answer or "".join(text_parts)
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request.model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "stop",
                }
            ],
        }

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[bytes]:
        """Stream the agent answer as OpenAI-compatible SSE chunks."""
        import time

        chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())
        buffer: list[str] = []
        emitted = False

        def frame(delta: dict[str, Any], finish: str | None = None) -> bytes:
            payload = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": request.model,
                "choices": [
                    {"index": 0, "delta": delta, "finish_reason": finish}
                ],
            }
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()

        async for event in self._agent_events(request):
            reasoning = event.get("reasoningContent")
            if reasoning:
                yield frame({"reasoning_content": reasoning})
            content = event.get("content")
            if content:
                buffer.append(content)

        # The agent emits one JSON document; unwrap it into the final answer.
        answer, thinking = _split_agent_payload("".join(buffer))
        if thinking:
            yield frame({"reasoning_content": thinking})
        if answer:
            emitted = True
            yield frame({"content": answer})
        if not emitted and not thinking:
            # Nothing parseable arrived — forward the raw text so the caller
            # still sees the upstream answer.
            raw = "".join(buffer)
            if raw:
                yield frame({"content": raw})
        yield frame({}, finish="stop")
        yield stream_done()

    async def _agent_events(self, request: ChatCompletionRequest) -> AsyncIterator[dict]:
        """Yield decoded ``data:`` objects from the agent SSE stream."""
        conversation_id = await self._ensure_conversation()
        url = f"{self.endpoint}/assistant/chat?{self._query()}"
        headers = await self._headers()
        body = self._build_body(request, conversation_id)
        async with self._client.stream("POST", url, json=body, headers=headers) as response:
            if response.status_code != 200:
                text = (await response.aread()).decode("utf-8", errors="replace")
                raise _parse_error(response.status_code, text)
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if not data or data == "[DONE]":
                    continue
                if data.startswith("[ERROR]"):
                    raise _parse_error(200, data)
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    if event.get("code") not in (None, 0):
                        raise _parse_error(200, data)
                    yield event

    async def close(self) -> None:
        await self._client.aclose()


# --------------------------------------------------------------- helpers


def _last_user_text(request: ChatCompletionRequest) -> str:
    for message in reversed(request.messages):
        if message.role == "user":
            return message.content or ""
    return ""


def _history_messages(request: ChatCompletionRequest) -> list[dict[str, Any]]:
    """Encode the OpenAI history as OrcaTerm ``historyMessages``.

    Each turn is stored the way the desktop client stores it: a ``human``
    message carrying an ``acp-prompt`` envelope and an ``ai`` message carrying
    the agent's own JSON answer. The last user turn travels as ``input``
    instead, so it is excluded here.
    """
    history: list[dict[str, Any]] = []
    messages = list(request.messages)
    # Drop the trailing user turn (delivered as ``input``).
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "user":
            del messages[index]
            break
    for message in messages:
        if message.role == "system":
            text = message.content or ""
            if text:
                history.append(
                    {
                        "type": "human",
                        "content": json.dumps(
                            {
                                "_format": "acp-prompt",
                                "version": 1,
                                "prompt": [{"type": "text", "text": text}],
                            },
                            ensure_ascii=False,
                        ),
                        "status": "success",
                        "id": uuid.uuid4().hex,
                    }
                )
        elif message.role == "user":
            history.append(
                {
                    "type": "human",
                    "content": json.dumps(
                        {
                            "_format": "acp-prompt",
                            "version": 1,
                            "prompt": [{"type": "text", "text": message.content or ""}],
                        },
                        ensure_ascii=False,
                    ),
                    "status": "success",
                    "id": uuid.uuid4().hex,
                }
            )
        elif message.role == "assistant" and message.content:
            history.append(
                {
                    "type": "ai",
                    "content": json.dumps(
                        {
                            "action": "completion",
                            "thinking": "",
                            "taskCompletion": message.content,
                        },
                        ensure_ascii=False,
                    ),
                    "status": "success",
                    "id": uuid.uuid4().hex,
                }
            )
    return history


def _split_agent_payload(text: str) -> tuple[str, str]:
    """Split the agent JSON document into ``(answer, thinking)``.

    The agent streams a fenced JSON object; when the payload cannot be parsed
    the raw text is returned as the answer so callers never lose content.
    """
    if not text:
        return "", ""
    candidate = _FENCE_RE.sub("", text.strip())
    # Trim any trailing fence fragment left by the regex alternation.
    if candidate.endswith("```"):
        candidate = candidate[:-3]
    try:
        decoded = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start >= 0 and end > start:
            try:
                decoded = json.loads(candidate[start : end + 1])
            except (json.JSONDecodeError, ValueError):
                return text.strip(), ""
        else:
            return text.strip(), ""
    if not isinstance(decoded, dict):
        return text.strip(), ""
    answer = decoded.get("taskCompletion") or decoded.get("content") or ""
    thinking = decoded.get("thinking") or ""
    return str(answer), str(thinking)


def _parse_error(status_code: int, text: str) -> OrcaTermError:
    """Map an upstream error body onto a typed OrcaTerm error."""
    clean = " ".join(text.strip().split())[:300]
    message = clean
    code: Any = None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if isinstance(payload, dict):
        code = payload.get("code")
        message = str(payload.get("message") or payload.get("error") or clean)
    lowered = message.lower()
    if "need_uin_and_skey" in lowered or "无访问此模型权限" in message:
        return OrcaTermAuthError(401, message)
    if "rate limit" in lowered or "chat-rpm" in lowered:
        return OrcaTermQuotaError(429, message)
    if "is not loaded" in message or "invalid conversationid" in lowered:
        return OrcaTermError(400, message)
    if code in (429,):
        return OrcaTermQuotaError(429, message)
    if code in (401, 403):
        return OrcaTermAuthError(int(code), message)
    return OrcaTermError(status_code, message)


__all__ = [
    "ORCATERM_ASSISTANT",
    "ORCATERM_MODE",
    "OrcaTermAuthError",
    "OrcaTermError",
    "OrcaTermProvider",
    "OrcaTermQuotaError",
]
