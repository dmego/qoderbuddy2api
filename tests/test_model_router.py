"""Tests for ModelRouter: cross-provider RR, cooldown, pre-commit failover."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import pytest

from qb2api.config import Settings
from qb2api.models import ModelCapabilities, ModelDefinition
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

    # after cooldown expires, the route is retried — advance both the router
    # and the provider pool clocks past the 30s cooldowns
    now = time.monotonic() + 1000
    monkeypatch.setattr("qb2api.worker.model_router.time.monotonic", lambda: now)
    monkeypatch.setattr("qb2api.providers.lb.time.monotonic", lambda: now)
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


def _dual_router(
    pools: dict[str, DynamicProviderPool],
    policies: dict[str, tuple],
    model_id: str = "deepseek-v4.1-flash",
) -> ModelRouter:
    """Router for a model served by both the international and domestic pools."""
    registry = ProviderRegistry()
    for name, pool in pools.items():
        registry.register(pool)
    routes = [
        ModelRoute("codebuddy", "deepseek-v4.1-flash"),
        ModelRoute("workbuddy_intl", "deepseek-v4.1-flash"),
    ]
    return ModelRouter(registry, _catalog(model_id, routes), policies)


def _intl_pools() -> tuple[dict[str, DynamicProviderPool], FakeProvider, FakeProvider]:
    domestic = FakeProvider("cb")
    intl = FakeProvider("wbintl")
    pools = {
        "codebuddy": _pool("codebuddy", domestic),
        "workbuddy_intl": _pool("workbuddy_intl", intl),
    }
    return pools, domestic, intl


@pytest.mark.asyncio
async def test_no_policy_keeps_plain_round_robin_across_providers():
    pools, domestic, intl = _intl_pools()
    router = _dual_router(pools, {})

    seen = {(await router.complete(_req("deepseek-v4.1-flash")))["id"] for _ in range(4)}

    assert seen == {"cb", "wbintl"}
    assert domestic.complete_calls == 2
    assert intl.complete_calls == 2


@pytest.mark.asyncio
async def test_priority_policy_sends_every_request_to_the_preferred_provider():
    """The requested behaviour: international first, domestic only as fallback."""
    from qb2api.route_policy import RoutePolicy

    pools, domestic, intl = _intl_pools()
    router = _dual_router(pools, {
        "deepseek-v4.1-flash": (
            RoutePolicy("workbuddy_intl", priority=0),
            RoutePolicy("codebuddy", priority=1),
        )
    })

    for _ in range(4):
        result = await router.complete(_req("deepseek-v4.1-flash"))
        assert result["id"] == "wbintl"

    assert intl.complete_calls == 4
    assert domestic.complete_calls == 0


@pytest.mark.asyncio
async def test_priority_policy_falls_back_to_the_domestic_account_on_failure():
    from qb2api.route_policy import RoutePolicy

    pools, domestic, intl = _intl_pools()
    intl.fail_before_chunk = True
    router = _dual_router(pools, {
        "deepseek-v4.1-flash": (
            RoutePolicy("workbuddy_intl", priority=0),
            RoutePolicy("codebuddy", priority=1),
        )
    })

    result = await router.complete(_req("deepseek-v4.1-flash"))

    assert result["id"] == "cb"
    assert intl.complete_calls == 1
    assert domestic.complete_calls == 1


@pytest.mark.asyncio
async def test_disabled_route_is_never_used_even_with_no_alternative():
    from qb2api.route_policy import RoutePolicy

    pools, domestic, intl = _intl_pools()
    router = _dual_router(pools, {
        "deepseek-v4.1-flash": (
            RoutePolicy("workbuddy_intl", priority=0),
            RoutePolicy("codebuddy", priority=0, weight=0),
        )
    })

    for _ in range(3):
        assert (await router.complete(_req("deepseek-v4.1-flash")))["id"] == "wbintl"
    assert domestic.complete_calls == 0


@pytest.mark.asyncio
async def test_weighted_same_tier_splits_traffic_between_providers():
    from qb2api.route_policy import RoutePolicy

    pools, domestic, intl = _intl_pools()
    router = _dual_router(pools, {
        "deepseek-v4.1-flash": (
            RoutePolicy("workbuddy_intl", weight=3),
            RoutePolicy("codebuddy", weight=1),
        )
    })

    for _ in range(8):
        await router.complete(_req("deepseek-v4.1-flash"))

    assert intl.complete_calls == 6
    assert domestic.complete_calls == 2


@pytest.mark.asyncio
async def test_policy_naming_an_unregistered_provider_is_ignored():
    """A preferred provider with no pool at all must not break routing."""
    from qb2api.route_policy import RoutePolicy

    domestic = FakeProvider("cb")
    intl = FakeProvider("wbintl")
    # Only the domestic pool is registered: the international route is absent.
    router = _dual_router(
        {"codebuddy": _pool("codebuddy", domestic)},
        {
            "deepseek-v4.1-flash": (
                RoutePolicy("workbuddy_intl", priority=0),
                RoutePolicy("codebuddy", priority=1),
            )
        },
    )

    assert (await router.complete(_req("deepseek-v4.1-flash")))["id"] == "cb"
    assert domestic.complete_calls == 1
    assert intl.complete_calls == 0


@pytest.mark.asyncio
async def test_empty_preferred_pool_falls_through_to_the_next_tier():
    """International selected but with no live account yet: serve domestically."""
    from qb2api.route_policy import RoutePolicy

    domestic = FakeProvider("cb")
    router = _dual_router(
        {
            "codebuddy": _pool("codebuddy", domestic),
            "workbuddy_intl": DynamicProviderPool(name="workbuddy_intl"),
        },
        {
            "deepseek-v4.1-flash": (
                RoutePolicy("workbuddy_intl", priority=0),
                RoutePolicy("codebuddy", priority=1),
            )
        },
    )

    assert (await router.complete(_req("deepseek-v4.1-flash")))["id"] == "cb"
    assert domestic.complete_calls == 1


@pytest.mark.asyncio
async def test_model_without_any_live_pool_is_unavailable():
    router = _dual_router(
        {"workbuddy_intl": DynamicProviderPool(name="workbuddy_intl")},
        {},
    )
    with pytest.raises(ProviderUnavailableError):
        await router.complete(_req("deepseek-v4.1-flash"))


@pytest.mark.asyncio
async def test_worker_runtime_publishes_snapshot_policies_into_the_router():
    """Regression: the Worker must actually apply the policies it was handed.

    A stored priority policy that never reaches ``ModelRouter`` silently
    degrades to round-robin, which is exactly the bug this covers.
    """
    from qb2api.route_policy import RoutePolicy
    from qb2api.runtime_snapshot import RuntimeSlot, RuntimeSnapshot
    from qb2api.worker.proxy_state import ProxyState

    domestic = FakeProvider("cb")
    intl = FakeProvider("wbintl")
    snapshot = RuntimeSnapshot(
        snapshot_version=1,
        codebuddy_endpoint="https://copilot.tencent.com",
        workbuddy_intl_endpoint="https://www.workbuddy.ai",
        qoder_timeout=300,
        models={
            "codebuddy": (
                _definition("deepseek-v4.1-flash", "codebuddy"),
            ),
            "workbuddy_intl": (
                _definition("deepseek-v4.1-flash", "workbuddy_intl"),
            ),
        },
        slots=(
            RuntimeSlot("codebuddy", "cb-1", 1, "domestic-token"),
            RuntimeSlot("workbuddy_intl", "wbintl-1", 1, "intl-token"),
        ),
        route_policies={
            "deepseek-v4.1-flash": (
                RoutePolicy("workbuddy_intl", priority=0),
                RoutePolicy("codebuddy", priority=1),
            )
        },
    )

    state = ProxyState(Settings())
    state.runtime = _StubRuntime(
        providers={"codebuddy": domestic, "workbuddy_intl": intl},
        policies=snapshot.route_policies,
    )
    state.registry.register(_pool("codebuddy", domestic))
    state.registry.register(_pool("workbuddy_intl", intl))
    state.model_definitions = {key: list(value) for key, value in snapshot.models.items()}
    state._rebuild_catalog()

    for _ in range(3):
        assert (await state.router.complete(_req("deepseek-v4.1-flash")))["id"] == "wbintl"
    assert domestic.complete_calls == 0


def _definition(model_id: str, provider: str):
    return ModelDefinition(model_id, model_id, provider, ModelCapabilities())


class _StubRuntime:
    """Minimal stand-in carrying just the fields ProxyState reads."""

    def __init__(self, *, providers: dict, policies: dict) -> None:
        self.route_policies = policies
        self._providers = providers
