"""Tests for ModelRouter: cross-provider RR, cooldown, pre-commit failover."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from qb2api.models import ModelCapabilities
from qb2api.models_catalog import ModelRoute, UnifiedModel
from qb2api.openai import ChatCompletionRequest
from qb2api.providers.base import Provider, ProviderRegistry
from qb2api.providers.lb import DynamicProviderPool, ProviderUnavailableError
from qb2api.worker.model_router import ModelRouter


class FakeProvider(Provider):
    name = "fake"

    def __init__(self, name: str, *, fail_before_chunk: bool = False, fail_after_chunk: bool = False):
        self.name = name
        self.fail_before_chunk = fail_before_chunk
        self.fail_after_chunk = fail_after_chunk
        self.complete_calls = 0
        self.stream_calls = 0
        self.seen_models: list[str] = []

    async def complete(self, request: ChatCompletionRequest) -> dict:
        self.complete_calls += 1
        self.seen_models.append(request.model)
        if self.fail_before_chunk:
            raise RuntimeError(f"{self.name}-fail")
        return {"id": self.name, "choices": [{"message": {"role": "assistant", "content": self.name}}]}

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[bytes]:
        self.stream_calls += 1
        self.seen_models.append(request.model)
        if self.fail_before_chunk:
            raise RuntimeError(f"{self.name}-pre")
        yield b"data: first\n\n"
        if self.fail_after_chunk:
            raise RuntimeError(f"{self.name}-post")
        yield b"data: [DONE]\n\n"

    async def close(self) -> None:
        pass


def _catalog(model_id: str, routes: list[ModelRoute]) -> dict[str, UnifiedModel]:
    entry = UnifiedModel(
        id=model_id,
        name=model_id,
        capabilities=ModelCapabilities(),
        max_context=128000,
        max_output=4096,
        routes=tuple(routes),
    )
    return {model_id: entry}


def _router(pools: dict[str, DynamicProviderPool], model_id: str = "deepseek-v4-flash") -> ModelRouter:
    registry = ProviderRegistry()
    for name, pool in pools.items():
        registry.register(pool)
    routes = [
        ModelRoute("codebuddy", "deepseek-v4-flash"),
        ModelRoute("qoder", "DeepSeek-V4-Flash"),
    ]
    return ModelRouter(registry, _catalog(model_id, routes))


def _req(model: str = "deepseek-v4-flash") -> ChatCompletionRequest:
    return ChatCompletionRequest(model=model, messages=[{"role": "user", "content": "hi"}])


def _pool(name: str, *providers: Provider) -> DynamicProviderPool:
    pool = DynamicProviderPool(name=name)
    pool._apply_slots_locked({str(i): p for i, p in enumerate(providers)})
    return pool


@pytest.mark.asyncio
async def test_round_robin_across_providers():
    cb = FakeProvider("cb")
    qd = FakeProvider("qd")
    router = _router({"codebuddy": _pool("codebuddy", cb), "qoder": _pool("qoder", qd)})

    r1 = await router.complete(_req())
    r2 = await router.complete(_req())

    assert {r1["id"], r2["id"]} == {"cb", "qd"}
    assert cb.seen_models == ["deepseek-v4-flash"]
    assert qd.seen_models == ["DeepSeek-V4-Flash"]


@pytest.mark.asyncio
async def test_failover_before_commit_uses_other_provider():
    bad = FakeProvider("cb", fail_before_chunk=True)
    good = FakeProvider("qd")
    router = _router({"codebuddy": _pool("codebuddy", bad), "qoder": _pool("qoder", good)})

    result = await router.complete(_req())

    assert result["id"] == "qd"
    assert bad.complete_calls == 1
    assert good.complete_calls == 1


@pytest.mark.asyncio
async def test_stream_no_failover_after_first_chunk():
    first = FakeProvider("cb", fail_after_chunk=True)
    second = FakeProvider("qd")
    router = _router({"codebuddy": _pool("codebuddy", first), "qoder": _pool("qoder", second)})

    chunks = []
    with pytest.raises(RuntimeError, match="cb-post"):
        async for c in router.stream(_req()):
            chunks.append(c)

    assert chunks == [b"data: first\n\n"]
    assert second.stream_calls == 0


@pytest.mark.asyncio
async def test_pool_without_slots_is_skipped():
    empty = DynamicProviderPool(name="codebuddy")
    good = FakeProvider("qd")
    router = _router({"codebuddy": empty, "qoder": _pool("qoder", good)})

    result = await router.complete(_req())
    assert result["id"] == "qd"


@pytest.mark.asyncio
async def test_all_routes_unavailable_raises():
    empty_cb = DynamicProviderPool(name="codebuddy")
    empty_qd = DynamicProviderPool(name="qoder")
    router = _router({"codebuddy": empty_cb, "qoder": empty_qd})

    with pytest.raises(ProviderUnavailableError):
        await router.complete(_req())


@pytest.mark.asyncio
async def test_unknown_model_raises():
    cb = FakeProvider("cb")
    router = _router({"codebuddy": _pool("codebuddy", cb)})

    with pytest.raises(ProviderUnavailableError):
        await router.complete(_req(model="totally-unknown"))


@pytest.mark.asyncio
async def test_single_route_passthrough_rewrites_upstream_id():
    cb = FakeProvider("cb")
    registry = ProviderRegistry()
    registry.register(_pool("codebuddy", cb))
    catalog = _catalog(
        "glm-5.1",
        [ModelRoute("codebuddy", "glm-5.1")],
    )
    router = ModelRouter(registry, catalog)

    result = await router.complete(_req(model="glm-5.1"))

    assert result["id"] == "cb"
    assert cb.seen_models == ["glm-5.1"]


@pytest.mark.asyncio
async def test_stream_restores_request_model_after_route():
    cb = FakeProvider("cb")
    router = _router({"codebuddy": _pool("codebuddy", cb), "qoder": _pool("qoder", FakeProvider("qd"))})
    request = _req()

    async for _ in router.stream(request):
        pass

    assert request.model == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_failed_route_is_cooled_down_for_retries(monkeypatch):
    flaky = FakeProvider("flaky", fail_before_chunk=True)
    good = FakeProvider("good")
    router = _router({"codebuddy": _pool("codebuddy", flaky), "qoder": _pool("qoder", good)})

    result = await router.complete(_req())
    assert result["id"] == "good"

    # within cooldown, the failed route is skipped entirely
    flaky.fail_before_chunk = False
    result2 = await router.complete(_req())
    assert result2["id"] == "good"
    assert flaky.complete_calls == 1

    # after cooldown expires, the route is retried
    monkeypatch.setattr("qb2api.worker.model_router.time.monotonic", lambda: 1_000_000)
    result3 = await router.complete(_req())
    assert result3["id"] in {"flaky", "good"}
    assert flaky.complete_calls == 2


def test_available_models_filters_unroutable_entries():
    registry = ProviderRegistry()
    registry.register(_pool("codebuddy", FakeProvider("cb")))
    registry.register(DynamicProviderPool(name="qoder"))
    catalog = _catalog(
        "glm-5.2",
        [ModelRoute("codebuddy", "glm-5.2"), ModelRoute("qoder", "GLM-5.2")],
    )
    router = ModelRouter(registry, catalog)
    assert [m.id for m in router.available_models()] == ["glm-5.2"]

    empty_registry = ProviderRegistry()
    empty_registry.register(DynamicProviderPool(name="codebuddy"))
    empty_registry.register(DynamicProviderPool(name="qoder"))
    router2 = ModelRouter(empty_registry, catalog)
    assert router2.available_models() == []
