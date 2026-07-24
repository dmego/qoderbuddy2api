"""Discovery and model metadata routes served by the Proxy Worker."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from qb2api.openai import ModelInfo, ModelListResponse

from .proxy_state import ProxyState

router = APIRouter()

_PROPS = {
    "provides": ["openai", "anthropic"],
    "capabilities": ["chat", "completion", "streaming", "tool_calls", "anthropic_messages"],
    "configuration": {
        "base_url": "http://localhost:9999/v1",
        "anthropic_base_url": "http://localhost:9999",
        "api_key": "optional",
    },
}


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    state = _state(request)
    return {"status": "ok", "providers": state.registry.providers}


@router.get("/version")
async def version() -> dict[str, str]:
    return {"version": "1.0.0", "component": "proxy-worker"}


@router.get("/v1/models")
async def list_models(request: Request) -> ModelListResponse:
    state = _state(request)
    data = [
        ModelInfo(id=f"{name}/{model.id}", owned_by=name)
        for name, definitions in state.available_models().items()
        for model in definitions
    ]
    return ModelListResponse(data=data)


@router.get("/api/v1/models")
@router.get("/api/tags")
async def list_ollama_models(request: Request) -> ModelListResponse:
    return await list_models(request)


@router.post("/api/show")
async def show_model() -> dict[str, dict[str, list[str]]]:
    return {"details": {"families": []}}


@router.get("/v1/props")
async def v1_props(request: Request) -> dict[str, Any]:
    state = _state(request)
    result = dict(_PROPS)
    result["models"] = {
        name: [model.id for model in definitions]
        for name, definitions in state.available_models().items()
    }
    return result


@router.get("/props")
async def props() -> dict[str, str]:
    return {}


@router.get("/v1/models/{model_path:path}")
async def model_info(model_path: str) -> dict[str, str | int]:
    return {
        "id": model_path,
        "object": "model",
        "created": 0,
        "owned_by": "qoderbuddy2api",
    }


def _state(request: Request) -> ProxyState:
    return request.app.state.proxy_state
