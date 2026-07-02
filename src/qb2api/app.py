"""FastAPI application for qoderbuddy2api."""

import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .anthropic import anthropic_to_openai, openai_to_anthropic
from .anthropic_stream import anthropic_error_sse, openai_stream_to_anthropic
from .config import Settings
from .logger import RequestLogger
from .models import ModelDefinition, load_models_from_config
from .openai import ChatCompletionRequest, ModelInfo, ModelListResponse
from .providers import ProviderRegistry
from .providers.base import Provider
from .providers.codebuddy import CodeBuddyError, CodeBuddyProvider
from .providers.lb import LoadBalancedProvider
from .providers.qoder import QoderError, QoderProvider

logger = logging.getLogger("qb2api")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
settings: Settings = None  # type: ignore
registry = ProviderRegistry()
request_logger: RequestLogger = None  # type: ignore
model_definitions: dict[str, list[ModelDefinition]] = {}

# /v1/props: expose capabilities for Claude Code / Codex auto-configuration
APP_PROPS = {
    "provides": ["openai", "anthropic"],
    "capabilities": ["chat", "completion", "streaming", "tool_calls", "anthropic_messages"],
    "configuration": {
        "base_url": "http://localhost:9999/v1",
        "anthropic_base_url": "http://localhost:9999",
        "api_key": "optional",
    },
    "models": {},
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: init providers, cleanup on shutdown."""
    global settings, request_logger, model_definitions, _model_index

    # Reset state (supports lifespan restart)
    registry.clear()
    _model_index = {}

    settings = Settings.from_env()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    request_logger = RequestLogger(settings.log_dir, settings.log_requests)

    # Load model configuration
    model_definitions = load_models_from_config(settings.model_config_path)

    # Initialize providers — supports multiple tokens
    if settings.codebuddy_tokens:
        instances = []
        for token in settings.codebuddy_tokens:
            instances.append(CodeBuddyProvider(
                token=token,
                endpoint=settings.codebuddy_endpoint,
            ))
        provider = LoadBalancedProvider(instances) if len(instances) > 1 else instances[0]
        registry.register(provider)
        models = model_definitions.get("codebuddy", [])
        plural = f" ({len(instances)} keys)" if len(instances) > 1 else ""
        logger.info(f"CodeBuddy: {len(models)} models{plural}")

    if settings.qoder_tokens:
        instances = []
        ok = 0
        for i, token in enumerate(settings.qoder_tokens):
            provider = QoderProvider(
                pat=token,
                timeout=settings.qoder_timeout,
            )
            try:
                await provider._ensure_session()
                instances.append(provider)
                ok += 1
                logger.info(f"Qoder[{i}]: COSY session established")
            except Exception as e:
                logger.error(f"Qoder[{i}]: COSY auth failed — {e}")
        if instances:
            final = LoadBalancedProvider(instances) if len(instances) > 1 else instances[0]
            registry.register(final)
            models = model_definitions.get("qoder", [])
            logger.info(f"Qoder: {len(models)} models ({ok}/{len(settings.qoder_tokens)} keys OK)")

    # Build model index for route validation
    _build_model_index()

    total = sum(len(v) for v in _available_models().values())
    logger.info(f"Total models: {total} | Providers: {registry.providers}")
    yield
    await registry.close_all()


# Model index: {provider_name: set(model_ids)}
_model_index: dict[str, set[str]] = {}


def _build_model_index():
    """Build {provider: set(model_ids)} index from registered providers and config."""
    global _model_index
    _model_index = {}
    for provider_name in registry.providers:
        models = model_definitions.get(provider_name, [])
        _model_index[provider_name] = {m.id for m in models}


def _available_models() -> dict[str, list[ModelDefinition]]:
    """Return models only for registered providers."""
    return {
        name: defs
        for name, defs in model_definitions.items()
        if name in registry.providers
    }


def _resolve_model(model: str) -> tuple[str, str]:
    """Resolve model string to (provider, model_id).

    - provider/model: validate provider exists AND model is in its list
    - bare model: check if exactly one provider has it, otherwise error
    """
    if "/" in model:
        provider_name, model_id = model.split("/", 1)
        if provider_name not in registry.providers:
            raise HTTPException(400, f"Unknown provider: {provider_name}. Available: {registry.providers}")
        if provider_name in _model_index and model_id not in _model_index[provider_name]:
            available = sorted(_model_index[provider_name])
            raise HTTPException(
                400,
                f"Unknown model '{model_id}' for provider '{provider_name}'. Available: {available}",
            )
        return provider_name, model_id

    # Bare model name — check all providers
    matches = []
    for pname, mids in _model_index.items():
        if model in mids:
            matches.append(pname)

    if len(matches) == 1:
        return matches[0], model
    elif len(matches) > 1:
        raise HTTPException(
            400,
            f"Ambiguous model '{model}' found in multiple providers: {matches}. Use provider/model prefix.",
        )
    else:
        all_models = [f"{p}/{m}" for p, mids in _model_index.items() for m in sorted(mids)]
        raise HTTPException(400, f"Unknown model: {model}. Available: {all_models[:10]}...")


app = FastAPI(title="qoderbuddy2api", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
# Public endpoints (no API key required)
PUBLIC_PATHS = {"/health", "/version", "/docs", "/openapi.json"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if settings and settings.api_key and request.url.path not in PUBLIC_PATHS:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != settings.api_key:
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "message": "Invalid or missing API key",
                        "type": "auth_error",
                        "code": "unauthorized",
                    }
                },
            )
    return await call_next(request)


@app.middleware("http")
async def log_middleware(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    elapsed = time.time() - start
    logger.info(f"{request.method} {request.url.path} -> {response.status_code} ({elapsed:.2f}s)")
    return response


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@app.exception_handler(json.JSONDecodeError)
async def json_error_handler(request: Request, exc: json.JSONDecodeError):
    return JSONResponse(
        status_code=400,
        content={"error": {"message": f"Invalid JSON: {exc}", "type": "invalid_request_error", "code": "invalid_json"}},
    )


def _json_error(status_code: int, message: str, error_type: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": error_type, "code": error_type}},
    )


def _current_settings() -> Settings:
    return settings or Settings.from_env()


def _config_snapshot() -> dict:
    current = _current_settings()
    return {
        "server": {
            "host": current.host,
            "port": current.port,
            "log_level": current.log_level,
        },
        "auth": {
            "api_key": current.mask_secret(current.api_key),
            "enabled": bool(current.api_key),
        },
        "providers": {
            "codebuddy": {
                "endpoint": current.codebuddy_endpoint,
                "token_count": len(current.codebuddy_tokens or []),
                "tokens": [current.mask_secret(token) for token in current.codebuddy_tokens or []],
            },
            "qoder": {
                "timeout_seconds": current.qoder_timeout,
                "token_count": len(current.qoder_tokens or []),
                "tokens": [current.mask_secret(token) for token in current.qoder_tokens or []],
            },
        },
        "logging": {
            "requests_enabled": current.log_requests,
            "log_dir": current.log_dir,
        },
        "model_config_path": current.model_config_path,
    }


def _serialize_env_value(value: Any) -> str:
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _quote_env_value(raw: str) -> str:
    """Quote env value if it contains characters that break dotenv parsing."""
    if any(c in raw for c in ("#", "=", "\n", "\r")) or (raw and (raw[0] == " " or raw[-1] == " ")):
        escaped = raw.replace('"', '\\"')
        return f'"{escaped}"'
    return raw


def _write_env_updates(updates: dict[str, Any]) -> Path:
    env_path = Path(os.getenv("QB2API_ENV_FILE", ".env"))
    existing_lines = env_path.read_text().splitlines() if env_path.exists() else []
    rendered = {key: f"{key}={_quote_env_value(_serialize_env_value(value))}" for key, value in updates.items()}
    output: list[str] = []
    seen: set[str] = set()

    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in rendered:
            output.append(rendered[key])
            seen.add(key)
        else:
            output.append(line)

    for key, line in rendered.items():
        if key not in seen:
            output.append(line)

    env_path.write_text("\n".join(output) + "\n")
    return env_path


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "providers": registry.providers}


@app.get("/version")
async def version():
    return {"version": "1.0.0"}


@app.get("/v1/models")
async def list_models():
    models = []
    for provider_name, defs in _available_models().items():
        for m in defs:
            models.append(ModelInfo(id=f"{m.provider}/{m.id}", owned_by=m.provider))
    return ModelListResponse(data=models)


@app.get("/api/v1/models")
async def list_models_ollama():
    """Ollama-compatible models endpoint."""
    return await list_models()


@app.get("/api/tags")
async def api_tags():
    """Ollama-compatible tags endpoint."""
    return await list_models()


@app.post("/api/show")
async def api_show():
    """Ollama-compatible show endpoint."""
    return {"details": {"families": []}}


@app.get("/v1/props")
async def v1_props():
    """Provider discovery endpoint — Claude Code / Codex auto-config."""
    props = dict(APP_PROPS)  # shallow copy
    props["models"] = {
        name: [m.id for m in defs]
        for name, defs in _available_models().items()
    }
    return props


@app.get("/props")
async def props():
    return {}


@app.get("/api/config")
async def get_config():
    """Return masked runtime configuration for local desktop clients."""
    return _config_snapshot()


@app.patch("/api/config")
async def patch_config(request: Request):
    """Persist supported configuration fields to `.env`.

    Provider token changes require restarting the service so provider sessions can
    be recreated without leaking old credentials.
    """
    try:
        body = json.loads(await request.body(), strict=False)
    except json.JSONDecodeError as e:
        return _json_error(400, f"Invalid JSON: {e}", "invalid_json")

    field_map = {
        "api_key": "QB2API_API_KEY",
        "codebuddy_token": "CODEBUDDY_TOKEN",
        "codebuddy_tokens": "CODEBUDDY_TOKEN",
        "qoder_token": "QODER_TOKEN",
        "qoder_tokens": "QODER_TOKEN",
        "log_requests": "QB2API_LOG_REQUESTS",
        "log_level": "QB2API_LOG_LEVEL",
        "host": "QB2API_HOST",
        "port": "QB2API_PORT",
        "qoder_timeout": "QODER_TIMEOUT",
        "codebuddy_endpoint": "CODEBUDDY_ENDPOINT",
    }
    updates = {
        env_key: body[field]
        for field, env_key in field_map.items()
        if field in body and body[field] is not None
    }
    if not updates:
        return _json_error(400, f"No supported config fields supplied. Supported: {sorted(field_map)}")

    env_path = _write_env_updates(updates)
    restart_required = any(key in updates for key in ("CODEBUDDY_TOKEN", "QODER_TOKEN", "CODEBUDDY_ENDPOINT"))
    return {
        "status": "ok",
        "env_file": str(env_path),
        "updated": sorted(updates),
        "restart_required": restart_required,
        "config": _config_snapshot(),
    }


@app.get("/v1/models/{model_path:path}")
async def get_model(model_path: str):
    """Get a specific model by ID."""
    return {
        "id": model_path,
        "object": "model",
        "created": 0,
        "owned_by": "qoderbuddy2api",
    }


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    """Anthropic Messages-compatible endpoint backed by the existing providers."""
    try:
        body = json.loads(await request.body(), strict=False)
    except json.JSONDecodeError as e:
        return _json_error(400, f"Invalid JSON: {e}", "invalid_json")

    try:
        openai_body = anthropic_to_openai(body)
        chat_request = ChatCompletionRequest(**openai_body)
    except Exception as e:
        raise HTTPException(400, f"Invalid Anthropic request: {e}")

    original_model = chat_request.model
    provider_name, model_id = _resolve_model(original_model)
    provider = registry.get(provider_name)
    if not provider:
        raise HTTPException(400, f"Provider not available: {provider_name}")

    chat_request.model = model_id
    start = time.time()
    effort = getattr(chat_request, "reasoning_effort", None)
    tool_calls_count = len(chat_request.tools) if chat_request.tools else 0

    try:
        if chat_request.stream:
            return StreamingResponse(
                _anthropic_stream_with_logging(
                    provider, chat_request, provider_name, original_model, effort, tool_calls_count
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        result = await provider.complete(chat_request)
        anthropic_response = openai_to_anthropic(result, model=original_model)
        duration = time.time() - start
        if request_logger:
            request_logger.log_request(
                model=model_id, provider=provider_name, stream=False,
                success=True, duration=duration,
                reasoning_effort=effort, tool_calls_count=tool_calls_count,
            )
        return JSONResponse(anthropic_response)

    except (CodeBuddyError, QoderError) as e:
        duration = time.time() - start
        status_code = getattr(e, "status_code", 502) or 502
        if not isinstance(status_code, int):
            status_code = 502
        if request_logger:
            request_logger.log_request(
                model=model_id, provider=provider_name, stream=False,
                success=False, duration=duration, error=str(e), status_code=status_code,
                reasoning_effort=effort, tool_calls_count=tool_calls_count,
            )
        return JSONResponse(
            status_code=status_code,
            content={"type": "error", "error": {"type": "api_error", "message": str(e)}},
        )
    except Exception as e:
        duration = time.time() - start
        if request_logger:
            request_logger.log_request(
                model=model_id, provider=provider_name, stream=False,
                success=False, duration=duration, error=str(e), status_code=502,
            )
        raise HTTPException(502, str(e))


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    # Parse JSON, reject malformed requests
    try:
        body = json.loads(await request.body(), strict=False)
    except json.JSONDecodeError as e:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": f"Invalid JSON: {e}",
                    "type": "invalid_request_error",
                    "code": "invalid_json",
                }
            },
        )

    try:
        chat_request = ChatCompletionRequest(**body)
    except Exception as e:
        raise HTTPException(400, f"Invalid request: {e}")

    model = chat_request.model
    provider_name, model_id = _resolve_model(model)

    provider = registry.get(provider_name)
    if not provider:
        raise HTTPException(400, f"Provider not available: {provider_name}")

    # Update resolved model name
    chat_request.model = model_id

    start = time.time()
    effort = getattr(chat_request, "reasoning_effort", None)
    tool_calls_count = len(chat_request.tools) if chat_request.tools else 0

    try:
        if chat_request.stream:
            return StreamingResponse(
                _stream_with_logging(provider, chat_request, provider_name, model, effort, tool_calls_count),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        else:
            result = await provider.complete(chat_request)
            duration = time.time() - start
            if request_logger:
                request_logger.log_request(
                    model=model_id, provider=provider_name, stream=False,
                    success=True, duration=duration,
                    reasoning_effort=effort, tool_calls_count=tool_calls_count,
                )
            return JSONResponse(result)

    except (CodeBuddyError, QoderError) as e:
        duration = time.time() - start
        status_code = getattr(e, "status_code", 502) or 502
        if not isinstance(status_code, int):
            status_code = 502
        if request_logger:
            request_logger.log_request(
                model=model_id, provider=provider_name, stream=False,
                success=False, duration=duration, error=str(e), status_code=status_code,
                reasoning_effort=effort, tool_calls_count=tool_calls_count,
            )
        return JSONResponse(
            status_code=status_code,
            content={"error": {"message": str(e), "type": "upstream_error", "code": "provider_error"}},
        )
    except Exception as e:
        duration = time.time() - start
        if request_logger:
            request_logger.log_request(
                model=model_id, provider=provider_name, stream=False,
                success=False, duration=duration, error=str(e), status_code=502,
            )
        raise HTTPException(502, str(e))


async def _stream_with_logging(
    provider: Provider,
    request: ChatCompletionRequest,
    provider_name: str,
    model: str,
    effort: str | None,
    tool_calls_count: int,
):
    """Stream with error handling and logging."""
    start = time.time()
    success = True
    error = None

    try:
        async for chunk in provider.stream(request):
            chunk_bytes = chunk if isinstance(chunk, bytes) else str(chunk).encode()
            yield chunk_bytes
    except (CodeBuddyError, QoderError) as e:
        success = False
        error = str(e)
        error_event = {"error": {"message": str(e), "type": "upstream_error"}}
        yield f"data: {json.dumps(error_event)}\n\n".encode()
        yield b"data: [DONE]\n\n"
    except Exception as e:
        success = False
        error = str(e)
        error_event = {"error": {"message": str(e), "type": "stream_error"}}
        yield f"data: {json.dumps(error_event)}\n\n".encode()
        yield b"data: [DONE]\n\n"
    finally:
        duration = time.time() - start
        if request_logger:
            request_logger.log_request(
                model=request.model, provider=provider_name, stream=True,
                success=success, duration=duration, error=error,
                reasoning_effort=effort, tool_calls_count=tool_calls_count,
            )


async def _anthropic_stream_with_logging(
    provider: Provider,
    request: ChatCompletionRequest,
    provider_name: str,
    original_model: str,
    effort: str | None,
    tool_calls_count: int,
):
    """Stream Anthropic events with provider error handling and monitoring."""
    start = time.time()
    success = True
    error = None

    try:
        async for event in openai_stream_to_anthropic(provider.stream(request), model=original_model):
            yield event
    except (CodeBuddyError, QoderError) as e:
        success = False
        error = str(e)
        yield anthropic_error_sse(str(e))
    except Exception as e:
        success = False
        error = str(e)
        yield anthropic_error_sse(str(e))
    finally:
        duration = time.time() - start
        if request_logger:
            request_logger.log_request(
                model=request.model, provider=provider_name, stream=True,
                success=success, duration=duration, error=error,
                reasoning_effort=effort, tool_calls_count=tool_calls_count,
            )
