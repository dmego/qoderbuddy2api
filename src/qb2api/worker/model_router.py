"""Per-model cross-provider routing: weighted priority tiers with failover.

Route order for one unified model id comes from the administrator's policy
(``priority`` tiers, ``weight`` inside a tier, per-route ``enabled``), falling
back to plain round-robin when no policy exists. Failover behaviour is
unchanged: a route is skipped while cooling down or when its pool is empty, and
once the first downstream chunk is committed the error propagates.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from ..openai import ChatCompletionRequest
from ..providers.base import Provider
from ..providers.lb import DynamicProviderPool, ProviderUnavailableError
from ..route_policy import RoutePolicy, RouteScheduler

logger = logging.getLogger("qb2api.worker.router")

_COOLDOWN_S = 30.0


@dataclass(frozen=True, slots=True)
class _Route:
    provider: str
    pool: Provider
    upstream_id: str


class ModelRouter(Provider):
    """Route one unified model id across provider pools."""

    name = "model-router"

    def __init__(
        self,
        registry: Any,
        catalog: dict[str, Any],
        policies: dict[str, tuple[RoutePolicy, ...]] | None = None,
    ) -> None:
        self._routes: dict[str, tuple[_Route, ...]] = {}
        self._schedulers: dict[str, RouteScheduler] = {}
        for model in catalog.values():
            routes = []
            for route in model.routes:
                pool = registry.get(route.provider)
                if pool is not None:
                    routes.append(_Route(route.provider, pool, route.upstream_id))
            if not routes:
                continue
            self._routes[model.id] = tuple(routes)
            self._schedulers[model.id] = RouteScheduler(
                _model_policies(model.id, routes, policies or {})
            )
        self._catalog = catalog
        self._cursor: dict[str, int] = {}
        self._cooldown_until: dict[tuple[str, str], float] = {}

    def available_models(self) -> list[Any]:
        return [model for model in self._catalog.values() if self._model_available(model.id)]

    def _model_available(self, model_id: str) -> bool:
        routes = self._routes.get(model_id)
        return bool(routes) and any(self._route_available(route) for route in routes)

    @staticmethod
    def _route_available(route: _Route) -> bool:
        if isinstance(route.pool, DynamicProviderPool) and not route.pool.has_available_slots:
            return False
        return True

    def _route_usable(self, model_id: str, route: _Route) -> bool:
        if not self._route_available(route):
            return False
        return time.monotonic() >= self._cooldown_until.get((model_id, route.provider), 0.0)

    def _ordered_routes(self, model_id: str, routes: tuple[_Route, ...]) -> tuple[_Route, ...]:
        scheduler = self._schedulers.get(model_id)
        if scheduler is None:
            return self._rotate(model_id, routes)
        providers = tuple(route.provider for route in routes)
        ranked = scheduler.order(providers)
        by_provider = {route.provider: route for route in routes}
        ordered = tuple(by_provider[name] for name in ranked if name in by_provider)
        if ordered:
            return ordered
        return self._rotate(model_id, routes)

    def _rotate(self, model_id: str, routes: tuple[_Route, ...]) -> tuple[_Route, ...]:
        start = self._cursor.get(model_id, 0) % len(routes)
        self._cursor[model_id] = (start + 1) % len(routes)
        return routes[start:] + routes[:start]

    def _mark_failed(self, model_id: str, route: _Route) -> None:
        self._cooldown_until[(model_id, route.provider)] = time.monotonic() + _COOLDOWN_S

    async def complete(self, request: ChatCompletionRequest) -> dict:
        model = request.model
        routes = self._routes.get(model)
        if not routes:
            raise ProviderUnavailableError(f"{model}: no available routes")
        last_err: Exception | None = None
        for route in self._ordered_routes(model, routes):
            if not self._route_usable(model, route):
                continue
            try:
                result = await self._complete_route(request, route)
                return result
            except Exception as error:
                self._mark_failed(model, route)
                last_err = error
                logger.warning("router %s[%s]: complete failed — %s", model, route.provider, error)
        if last_err is not None:
            raise last_err
        raise ProviderUnavailableError(f"{model}: no available routes")

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[bytes]:
        model = request.model
        routes = self._routes.get(model)
        if not routes:
            raise ProviderUnavailableError(f"{model}: no available routes")
        last_err: Exception | None = None
        for route in self._ordered_routes(model, routes):
            if not self._route_usable(model, route):
                continue
            try:
                async for chunk in self._stream_route(request, route):
                    yield chunk
                return
            except Exception as error:
                if request.telemetry["stream_committed"]:
                    raise
                self._mark_failed(model, route)
                last_err = error
                logger.warning("router %s[%s]: stream failed pre-commit — %s", model, route.provider, error)
        if last_err is not None:
            raise last_err
        raise ProviderUnavailableError(f"{model}: no available routes")

    async def _complete_route(self, request: ChatCompletionRequest, route: _Route) -> dict:
        original = request.model
        request.model = route.upstream_id
        request.record_provider(route.provider)
        try:
            return await route.pool.complete(request)
        finally:
            request.model = original

    async def _stream_route(
        self,
        request: ChatCompletionRequest,
        route: _Route,
    ) -> AsyncIterator[bytes]:
        original = request.model
        request.model = route.upstream_id
        request.record_provider(route.provider)
        try:
            async for chunk in route.pool.stream(request):
                yield chunk
        finally:
            request.model = original

    async def close(self) -> None:
        """Pools are owned by the runtime; nothing to close here."""


def _model_policies(
    model_id: str,
    routes: tuple[_Route, ...],
    policies: dict[str, tuple[RoutePolicy, ...]],
) -> dict[str, RoutePolicy]:
    """Policies for one model, defaulting to a single equal-weight tier."""
    providers = tuple(route.provider for route in routes)
    stored = policies.get(model_id) or ()
    known = {policy.provider for policy in stored}
    selected = {policy.provider: policy for policy in stored if policy.provider in providers}
    for provider in providers:
        if provider not in known:
            selected[provider] = RoutePolicy(provider=provider)
    return selected
