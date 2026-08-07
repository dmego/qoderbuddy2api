"""Per-model cross-provider routing: round-robin, cooldown, pre-commit failover."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from ..openai import ChatCompletionRequest
from ..providers.base import Provider
from ..providers.lb import DynamicProviderPool, ProviderUnavailableError

logger = logging.getLogger("qb2api.worker.router")

_COOLDOWN_S = 30.0


@dataclass(frozen=True, slots=True)
class _Route:
    provider: str
    pool: Provider
    upstream_id: str


class ModelRouter(Provider):
    """Route one unified model id across provider pools.

    - Round-robin start per model, advancing only after success.
    - A route is skipped while cooling down (30s) or when its provider pool
      has no available slots.
    - Failover happens only before the first downstream chunk; once
      ``request.telemetry["stream_committed"]`` is true the error propagates.
    - The upstream model id is rewritten per route and restored afterwards.
    """

    name = "model-router"

    def __init__(self, registry: Any, catalog: dict[str, Any]) -> None:
        self._routes: dict[str, tuple[_Route, ...]] = {}
        for model in catalog.values():
            routes = []
            for route in model.routes:
                pool = registry.get(route.provider)
                if pool is not None:
                    routes.append(_Route(route.provider, pool, route.upstream_id))
            if routes:
                self._routes[model.id] = tuple(routes)
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
        start = self._cursor.get(model_id, 0) % len(routes)
        return routes[start:] + routes[:start]

    def _advance(self, model_id: str, routes: tuple[_Route, ...]) -> None:
        self._cursor[model_id] = (self._cursor.get(model_id, 0) + 1) % len(routes)

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
                self._advance(model, routes)
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
                self._advance(model, routes)
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
