"""Qoder CN provider using per-account COSY sessions and direct SSE HTTP."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

import httpx

from ..openai import ChatCompletionRequest, stream_chunk, stream_done
from .base import Provider
from .qoder_auth import (
    CHAT_PATH,
    CHAT_QUERY,
    GATEWAY,
    QoderError,
    QoderSession,
)
from .qoder_payload import (
    QODER_CLI_MODEL_KEYS as QODER_CLI_MODEL_KEYS,
)
from .qoder_payload import (
    build_qoder_payload,
    parse_qoder_sse_data,
    qoder_encode,
    qoder_model_key,
)

logger = logging.getLogger("qb2api")

# Backward-compatible private exports used by existing callers/tests.
_qoder_encode = qoder_encode
_qoder_model_key = qoder_model_key


class QoderProvider(Provider):
    name = "qoder"

    def __init__(self, pat: str, timeout: int = 300, **_kwargs):
        self.pat = pat
        self.timeout = timeout
        self._session: QoderSession | None = None
        self._session_lock = asyncio.Lock()

    async def _ensure_session(
        self,
        *,
        force: bool = False,
        stale: QoderSession | None = None,
    ) -> QoderSession:
        async with self._session_lock:
            current = self._session
            if force and stale is not None and current is not stale:
                if current is not None and current._ready:
                    return current
            if not force and current is not None and current._ready:
                return current
            candidate = QoderSession(self.pat)
            try:
                await candidate.authenticate()
            except BaseException:
                await candidate.close()
                raise
            self._session = candidate
            if current is not None:
                await current.close()
            return candidate

    async def complete(self, request: ChatCompletionRequest) -> dict:
        from ..sse import StreamAggregator

        aggregator = StreamAggregator(model=request.model)
        async for chunk in self.stream(request):
            if chunk.startswith(b"data: [DONE]"):
                break
            try:
                payload = json.loads(chunk[6:].decode().strip())
                if payload:
                    aggregator.process(payload)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
        request.observe_usage(aggregator.usage)
        return aggregator.response()

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[bytes]:
        session = await self._ensure_session()
        emitted = False
        for attempt in range(2):
            try:
                async for chunk in self._stream_once(request, session):
                    emitted = emitted or bool(chunk)
                    yield chunk
                session.mark_success()
                yield stream_done()
                return
            except QoderError as error:
                can_reauthenticate = (
                    not emitted
                    and attempt == 0
                    and error.status_code in {401, 403}
                )
                if not can_reauthenticate:
                    raise
                session.invalidate("upstream_auth_failed")
                session = await self._ensure_session(force=True, stale=session)

    async def _stream_once(
        self,
        request: ChatCompletionRequest,
        session: QoderSession,
    ) -> AsyncIterator[bytes]:
        model = request.model
        upstream_model = qoder_model_key(model)
        payload = build_qoder_payload(request, model)
        encoded = qoder_encode(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        headers = session.chat_headers(encoded, upstream_model)
        url = f"{GATEWAY}{CHAT_PATH}?{CHAT_QUERY}"
        timeout = httpx.Timeout(self.timeout, connect=15)
        chunk_count = 0
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                url,
                content=encoded.encode("utf-8"),
                headers=headers,
            ) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise QoderError(
                        f"Qoder chat failed (HTTP {response.status_code})",
                        status_code=response.status_code,
                    )
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    parsed = parse_qoder_sse_data(line[5:].strip())
                    if parsed is None:
                        continue
                    delta, finish_reason = parsed
                    if delta or finish_reason:
                        chunk_count += 1
                        yield stream_chunk(
                            model,
                            delta,
                            finish_reason=finish_reason,
                        )
        logger.info("Qoder: stream completed with %s chunks", chunk_count)

    def _build_payload(self, request: ChatCompletionRequest, model: str) -> dict:
        return build_qoder_payload(request, model)

    async def close(self) -> None:
        async with self._session_lock:
            session = self._session
            self._session = None
        if session is not None:
            await session.close()
