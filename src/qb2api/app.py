"""FastAPI application for qoderbuddy2api."""

import json
import time
import uuid
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

from .config import Settings
from .openai import ChatCompletionRequest, ModelInfo, ModelListResponse
from .providers import ProviderRegistry
from .providers.base import Provider
from .providers.codebuddy import CodeBuddyProvider, CodeBuddyError
from .providers.qoder import QoderProvider, QoderError
from .providers.lb import LoadBalancedProvider
from .models import load_models_from_config, ModelDefinition
from .logger import RequestLogger
from .cache import ResponseCache

logger = logging.getLogger("qb2api")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
settings: Settings = None  # type: ignore
registry = ProviderRegistry()
request_logger: RequestLogger = None  # type: ignore
model_definitions: dict[str, list[ModelDefinition]] = {}
response_cache: ResponseCache = ResponseCache(max_size=200, ttl=300)

# /v1/props: expose capabilities for Claude Code / Codex auto-configuration
APP_PROPS = {
    "provides": "openai",
    "capabilities": ["chat", "completion", "streaming", "tool_calls"],
    "configuration": {
        "base_url": "http://localhost:9999/v1",
        "api_key": "optional",
    },
    "models": {},
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: init providers, cleanup on shutdown."""
    global settings, request_logger, model_definitions, _model_index, response_cache

    # Reset state (supports lifespan restart)
    registry.clear()
    _model_index = {}

    settings = Settings.from_env()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    response_cache = ResponseCache(max_size=settings.cache_max_size, ttl=settings.cache_ttl)
    logger.info(f"Cache: max={settings.cache_max_size} ttl={settings.cache_ttl}s enabled={settings.cache_enabled}")

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
            raise HTTPException(400, f"Unknown model '{model_id}' for provider '{provider_name}'. Available: {available}")
        return provider_name, model_id

    # Bare model name — check all providers
    matches = []
    for pname, mids in _model_index.items():
        if model in mids:
            matches.append(pname)

    if len(matches) == 1:
        return matches[0], model
    elif len(matches) > 1:
        raise HTTPException(400, f"Ambiguous model '{model}' found in multiple providers: {matches}. Use provider/model prefix.")
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
                content={"error": {"message": "Invalid or missing API key", "type": "auth_error", "code": "unauthorized"}},
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


@app.get("/v1/models/{model_path:path}")
async def get_model(model_path: str):
    """Get a specific model by ID."""
    return {
        "id": model_path,
        "object": "model",
        "created": 0,
        "owned_by": "qoderbuddy2api",
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    # Parse JSON, reject malformed requests
    try:
        body = json.loads(await request.body(), strict=False)
    except json.JSONDecodeError as e:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": f"Invalid JSON: {e}", "type": "invalid_request_error", "code": "invalid_json"}},
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

    # Non-streaming cache check
    if not chat_request.stream and settings.cache_enabled:
        cached = response_cache.get(body)
        if cached:
            logger.info(f"✅ cache/{provider_name}/{model_id} sync {time.time()-start:.2f}s")
            cached["id"] = f"chatcmpl-{uuid.uuid4().hex[:12]}"
            cached["created"] = int(time.time())
            return JSONResponse(cached)

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
            request_logger.log_request(
                model=model_id, provider=provider_name, stream=False,
                success=True, duration=duration,
                reasoning_effort=effort, tool_calls_count=tool_calls_count,
            )
            # Cache non-streaming successful responses
            if settings.cache_enabled:
                response_cache.set(body, result)
            return JSONResponse(result)

    except (CodeBuddyError, QoderError) as e:
        duration = time.time() - start
        status_code = getattr(e, "status_code", 502) or 502
        if not isinstance(status_code, int):
            status_code = 502
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
            yield chunk
    except (CodeBuddyError, QoderError) as e:
        success = False
        error = str(e)
        # Emit error event (not a fake assistant message)
        error_event = {
            "error": {"message": str(e), "type": "upstream_error"},
        }
        yield f"data: {json.dumps(error_event)}\n\n".encode()
        yield b"data: [DONE]\n\n"
    except Exception as e:
        success = False
        error = str(e)
        error_event = {
            "error": {"message": str(e), "type": "stream_error"},
        }
        yield f"data: {json.dumps(error_event)}\n\n".encode()
        yield b"data: [DONE]\n\n"
    finally:
        duration = time.time() - start
        request_logger.log_request(
            model=request.model, provider=provider_name, stream=True,
            success=success, duration=duration, error=error,
            reasoning_effort=effort, tool_calls_count=tool_calls_count,
        )
