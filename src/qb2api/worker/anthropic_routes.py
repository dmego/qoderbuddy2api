"""Anthropic Messages API route owned by the Proxy Worker."""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from qb2api.anthropic import anthropic_to_openai, openai_to_anthropic
from qb2api.openai import ChatCompletionRequest
from qb2api.providers.codebuddy import CodeBuddyError
from qb2api.providers.lb import ProviderUnavailableError
from qb2api.providers.qoder import QoderError

from .proxy_state import ProxyState
from .streaming import StreamLogContext, anthropic_stream

router = APIRouter()


@router.post("/v1/messages", response_model=None)
async def messages(request: Request) -> JSONResponse | StreamingResponse:
    body = await _body_or_error(request)
    if isinstance(body, JSONResponse):
        return body
    try:
        chat_request = ChatCompletionRequest(**anthropic_to_openai(body))
    except Exception as error:
        raise HTTPException(400, f"Invalid Anthropic request: {error}") from error
    state = _state(request)
    original_model = chat_request.model
    resolved = state.resolve_model(original_model)
    provider = resolved.provider
    chat_request.model = resolved.upstream_model
    if resolved.provider_name is not None:
        chat_request.record_provider(resolved.provider_name)
    request.state.telemetry_context = {
        "model_id": resolved.canonical_id,
        "protocol": "anthropic",
        "chat_request": chat_request,
    }
    context = StreamLogContext(
        provider_name=resolved.provider_name or "",
        model=original_model,
        reasoning_effort=getattr(chat_request, "reasoning_effort", None),
        tool_calls_count=len(chat_request.tools or []),
    )
    if chat_request.stream:
        return StreamingResponse(
            anthropic_stream(
                provider=provider,
                request=chat_request,
                original_model=original_model,
                context=context,
                request_logger=state.request_logger,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return await _complete(
        provider=provider,
        request=chat_request,
        original_model=original_model,
        context=context,
        state=state,
    )


async def _body_or_error(request: Request) -> dict[str, Any] | JSONResponse:
    try:
        return json.loads(await request.body(), strict=False)
    except json.JSONDecodeError as error:
        return _error_response(400, f"Invalid JSON: {error}")


async def _complete(
    *,
    provider: Any,
    request: ChatCompletionRequest,
    original_model: str,
    context: StreamLogContext,
    state: ProxyState,
) -> JSONResponse:
    started = time.time()
    try:
        result = await provider.complete(request)
    except ProviderUnavailableError as error:
        raise HTTPException(503, str(error)) from error
    except (CodeBuddyError, QoderError) as error:
        _log(
            state=state,
            request=request,
            context=context,
            success=False,
            error=str(error),
            started=started,
        )
        return _error_response(_status_code(error), str(error))
    except Exception as error:
        _log(
            state=state,
            request=request,
            context=context,
            success=False,
            error=str(error),
            started=started,
        )
        raise HTTPException(502, str(error)) from error
    _log(
        state=state,
        request=request,
        context=context,
        success=True,
        error=None,
        started=started,
    )
    return JSONResponse(openai_to_anthropic(result, model=original_model))


def _error_response(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"type": "error", "error": {"type": "api_error", "message": message}},
    )


def _status_code(error: Exception) -> int:
    value = getattr(error, "status_code", 502)
    return value if isinstance(value, int) else 502


def _log(
    *,
    state: ProxyState,
    request: ChatCompletionRequest,
    context: StreamLogContext,
    success: bool,
    error: str | None,
    started: float,
) -> None:
    if state.request_logger is None:
        return
    state.request_logger.log_request(
        model=request.model,
        provider=context.provider_name or request.telemetry.get("provider") or "unknown",
        stream=False,
        success=success,
        duration=time.time() - started,
        error=error,
        reasoning_effort=context.reasoning_effort,
        tool_calls_count=context.tool_calls_count,
    )


def _state(request: Request) -> ProxyState:
    return request.app.state.proxy_state
