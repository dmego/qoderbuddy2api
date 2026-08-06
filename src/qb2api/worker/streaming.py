"""Protocol stream adapters with request logging for the Proxy Worker."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from qb2api.anthropic_stream import anthropic_error_sse, openai_stream_to_anthropic
from qb2api.logger import RequestLogger
from qb2api.openai import ChatCompletionRequest
from qb2api.providers.base import Provider
from qb2api.providers.codebuddy import CodeBuddyError
from qb2api.providers.lb import ProviderUnavailableError
from qb2api.providers.qoder import QoderError


@dataclass(frozen=True, slots=True)
class StreamLogContext:
    provider_name: str
    model: str
    reasoning_effort: str | None
    tool_calls_count: int


async def openai_stream(
    *,
    provider: Provider,
    request: ChatCompletionRequest,
    context: StreamLogContext,
    request_logger: RequestLogger | None,
    keep_reasoning: bool | None = None,
    settings: Any | None = None,
) -> AsyncIterator[bytes]:
    start = time.time()
    success, error = True, None
    keep = keep_reasoning if keep_reasoning is not None else getattr(settings, "stream_reasoning", False)
    try:
        async for chunk in provider.stream(request):
            raw = chunk if isinstance(chunk, bytes) else str(chunk).encode()
            yield _filter_reasoning(raw, keep=keep)
    except ProviderUnavailableError as exc:
        success, error = False, str(exc)
        yield _openai_error(error, "provider_unavailable")
    except (CodeBuddyError, QoderError) as exc:
        success, error = False, str(exc)
        yield _openai_error(error, "upstream_error")
    except Exception as exc:
        success, error = False, str(exc)
        yield _openai_error(error, "stream_error")
    finally:
        _log(
            request_logger=request_logger,
            request=request,
            context=context,
            success=success,
            error=error,
            duration=time.time() - start,
        )


async def anthropic_stream(
    *,
    provider: Provider,
    request: ChatCompletionRequest,
    original_model: str,
    context: StreamLogContext,
    request_logger: RequestLogger | None,
) -> AsyncIterator[bytes]:
    start = time.time()
    success, error = True, None
    try:
        async for event in openai_stream_to_anthropic(provider.stream(request), model=original_model):
            yield event
    except Exception as exc:
        success, error = False, str(exc)
        yield anthropic_error_sse(error)
    finally:
        _log(
            request_logger=request_logger,
            request=request,
            context=context,
            success=success,
            error=error,
            duration=time.time() - start,
        )


def _filter_reasoning(chunk: bytes, *, keep: bool) -> bytes:
    if keep:
        return chunk
    line = chunk.decode("utf-8", errors="replace").strip()
    if not line.startswith("data:") or line.startswith("data: [DONE]"):
        return chunk
    try:
        payload = json.loads(line[5:].strip())
    except json.JSONDecodeError:
        return chunk
    for choice in payload.get("choices", []):
        delta = choice.get("delta")
        if isinstance(delta, dict):
            delta.pop("reasoning_content", None)
    return f"data: {json.dumps(payload)}\n\n".encode()


def _openai_error(message: str, error_type: str) -> bytes:
    body = {"error": {"message": message, "type": error_type}}
    return f"data: {json.dumps(body)}\n\n".encode()


def _log(
    *,
    request_logger: RequestLogger | None,
    request: ChatCompletionRequest,
    context: StreamLogContext,
    success: bool,
    error: str | None,
    duration: float,
) -> None:
    if request_logger is None:
        return
    request_logger.log_request(
        model=request.model,
        provider=context.provider_name,
        stream=True,
        success=success,
        duration=duration,
        error=error,
        reasoning_effort=context.reasoning_effort,
        tool_calls_count=context.tool_calls_count,
    )
