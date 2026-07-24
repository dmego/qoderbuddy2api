"""OpenAI-compatible chat routes owned by the Proxy Worker."""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from qb2api.openai import ChatCompletionRequest
from qb2api.providers.codebuddy import CodeBuddyError
from qb2api.providers.lb import ProviderUnavailableError
from qb2api.providers.qoder import QoderError

from .proxy_state import ProxyState
from .streaming import StreamLogContext, openai_stream

router = APIRouter()


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: Request) -> JSONResponse | StreamingResponse:
    body = await _body_or_error(request)
    if isinstance(body, JSONResponse):
        return body
    try:
        chat_request = ChatCompletionRequest(**body)
    except Exception as error:
        raise HTTPException(400, f"Invalid request: {error}") from error
    state = _state(request)
    original_model = chat_request.model
    provider_name, model_id = state.resolve_model(original_model)
    provider = state.registry.get(provider_name)
    if provider is None:
        raise HTTPException(400, f"Provider not available: {provider_name}")
    chat_request.model = model_id
    request.state.telemetry_context = _telemetry_context(provider_name, model_id, chat_request)
    context = StreamLogContext(
        provider_name=provider_name,
        model=original_model,
        reasoning_effort=getattr(chat_request, "reasoning_effort", None),
        tool_calls_count=len(chat_request.tools or []),
    )
    if chat_request.stream:
        return StreamingResponse(
            openai_stream(
                provider=provider,
                request=chat_request,
                context=context,
                request_logger=state.request_logger,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return await _complete(
        provider=provider,
        chat_request=chat_request,
        context=context,
        state=state,
    )


async def _body_or_error(request: Request) -> dict[str, Any] | JSONResponse:
    try:
        return json.loads(await request.body(), strict=False)
    except json.JSONDecodeError as error:
        return _error_response(
            400,
            f"Invalid JSON: {error}",
            "invalid_json",
            error_type="invalid_request_error",
        )


async def _complete(
    *,
    provider: Any,
    chat_request: ChatCompletionRequest,
    context: StreamLogContext,
    state: ProxyState,
) -> JSONResponse:
    started = time.time()
    try:
        result = await provider.complete(chat_request)
    except ProviderUnavailableError as error:
        raise HTTPException(503, str(error)) from error
    except (CodeBuddyError, QoderError) as error:
        _log_failure(
            state=state,
            request=chat_request,
            context=context,
            error=error,
            started=started,
        )
        return _error_response(_status_code(error), str(error), "provider_error")
    except Exception as error:
        _log_failure(
            state=state,
            request=chat_request,
            context=context,
            error=error,
            started=started,
        )
        raise HTTPException(502, str(error)) from error
    _log_success(
        state=state,
        request=chat_request,
        context=context,
        started=started,
    )
    return JSONResponse(result)


def _telemetry_context(
    provider_name: str,
    model_id: str,
    chat_request: ChatCompletionRequest,
) -> dict[str, Any]:
    return {
        "provider": provider_name,
        "model_id": model_id,
        "protocol": "openai",
        "chat_request": chat_request,
    }


def _error_response(
    status_code: int,
    message: str,
    code: str,
    *,
    error_type: str = "upstream_error",
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": error_type, "code": code}},
    )


def _status_code(error: Exception) -> int:
    value = getattr(error, "status_code", 502)
    return value if isinstance(value, int) else 502


def _log_success(
    *,
    state: ProxyState,
    request: ChatCompletionRequest,
    context: StreamLogContext,
    started: float,
) -> None:
    _log_request(
        state=state,
        request=request,
        context=context,
        success=True,
        duration=time.time() - started,
    )


def _log_failure(
    *,
    state: ProxyState,
    request: ChatCompletionRequest,
    context: StreamLogContext,
    error: Exception,
    started: float,
) -> None:
    _log_request(
        state=state,
        request=request,
        context=context,
        success=False,
        duration=time.time() - started,
        error=str(error),
        status_code=_status_code(error),
    )


def _log_request(
    state: ProxyState,
    request: ChatCompletionRequest,
    context: StreamLogContext,
    *,
    success: bool,
    duration: float,
    error: str | None = None,
    status_code: int | None = None,
) -> None:
    if state.request_logger is None:
        return
    state.request_logger.log_request(
        model=request.model,
        provider=context.provider_name,
        stream=False,
        success=success,
        duration=duration,
        error=error,
        status_code=status_code,
        reasoning_effort=context.reasoning_effort,
        tool_calls_count=context.tool_calls_count,
    )


def _state(request: Request) -> ProxyState:
    return request.app.state.proxy_state
