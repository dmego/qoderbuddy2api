"""Unified model catalog: canonical lowercase ids merged across providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import ModelCapabilities, ModelDefinition

PROVIDER_ORDER = ("codebuddy", "qoder")


def normalize_model_id(model_id: str) -> str:
    """Canonical id form: lowercased, stripped of surrounding whitespace."""
    return model_id.strip().lower()


@dataclass(frozen=True, slots=True)
class ModelRoute:
    """One provider backend that can serve a unified model."""

    provider: str
    upstream_id: str


@dataclass(frozen=True, slots=True)
class UnifiedModel:
    """Public model entry with one or more provider routes."""

    id: str
    name: str
    capabilities: ModelCapabilities
    max_context: int
    max_output: int
    routes: tuple[ModelRoute, ...] = field(default_factory=tuple)

    def to_info(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "model",
            "created": 0,
            "owned_by": "qoderbuddy2api",
        }

    def route_for(self, provider: str) -> ModelRoute | None:
        for route in self.routes:
            if route.provider == provider:
                return route
        return None

    def canonicalize(self, provider: str, upstream_id: str) -> str | None:
        """Return the canonical id when (provider, upstream_id) is one of our routes."""
        route = self.route_for(provider)
        if route is not None and route.upstream_id == upstream_id:
            return self.id
        return None


def build_unified_catalog(
    per_provider: dict[str, list[ModelDefinition]],
    overrides: dict[str, Any] | None = None,
) -> dict[str, UnifiedModel]:
    """Merge per-provider model definitions into one unified catalog.

    - Models whose normalized (lowercased) ids match across providers are
      merged into a single entry with one route per provider.
    - Capabilities are OR-merged; max_context/max_output take the maximum.
    - ``overrides`` keyed by canonical id may replace name, routes, or
      capabilities for a given entry (``unified`` section of models.json).
    """
    entries: dict[str, list[ModelDefinition]] = {}
    for provider, definitions in per_provider.items():
        for definition in definitions:
            canonical = normalize_model_id(definition.id)
            entries.setdefault(canonical, []).append(definition)

    merged = {
        canonical: _merge_entries(canonical, definitions)
        for canonical, definitions in entries.items()
    }
    for canonical, override in (overrides or {}).items():
        merged[canonical] = _apply_override(
            canonical,
            merged.get(canonical),
            override,
        )
    return dict(sorted(merged.items()))


def _merge_entries(
    canonical: str,
    definitions: list[ModelDefinition],
) -> UnifiedModel:
    ordered = sorted(
        definitions,
        key=lambda d: PROVIDER_ORDER.index(d.provider)
        if d.provider in PROVIDER_ORDER
        else len(PROVIDER_ORDER),
    )
    capabilities = _union_capabilities([d.capabilities for d in definitions])
    name = _preferred_name(ordered)
    routes = tuple(
        ModelRoute(provider=d.provider, upstream_id=d.id) for d in ordered
    )
    return UnifiedModel(
        id=canonical,
        name=name,
        capabilities=capabilities,
        max_context=max(d.max_context for d in definitions),
        max_output=max(d.max_output for d in definitions),
        routes=routes,
    )


def _apply_override(
    canonical: str,
    base: UnifiedModel | None,
    override: dict[str, Any],
) -> UnifiedModel:
    name = override.get("name") or (base.name if base else canonical)
    return UnifiedModel(
        id=canonical,
        name=str(name),
        capabilities=_override_capabilities(base, override),
        max_context=base.max_context if base else 128000,
        max_output=base.max_output if base else 4096,
        routes=_override_routes(base, override),
    )


def _override_capabilities(
    base: UnifiedModel | None,
    override: dict[str, Any],
) -> ModelCapabilities:
    raw = override.get("capabilities")
    if isinstance(raw, dict):
        return _capabilities_from_mapping(raw)
    return base.capabilities if base else ModelCapabilities()


def _override_routes(
    base: UnifiedModel | None,
    override: dict[str, Any],
) -> tuple[ModelRoute, ...]:
    raw = override.get("routes")
    if isinstance(raw, list) and raw:
        return tuple(
            ModelRoute(
                provider=str(item["provider"]),
                upstream_id=str(item["upstream_id"]),
            )
            for item in raw
            if isinstance(item, dict) and item.get("provider") and item.get("upstream_id")
        )
    return base.routes if base else ()


def _union_capabilities(
    values: list[ModelCapabilities],
) -> ModelCapabilities:
    return ModelCapabilities(
        chat=any(v.chat for v in values),
        streaming=any(v.streaming for v in values),
        tool_calling=any(v.tool_calling for v in values),
        reasoning=any(v.reasoning for v in values),
        reasoning_effort=any(v.reasoning_effort for v in values),
        context_window=any(v.context_window for v in values),
        max_output_tokens=any(v.max_output_tokens for v in values),
    )


def _capabilities_from_mapping(value: dict[str, Any]) -> ModelCapabilities:
    names = (
        "chat", "streaming", "tool_calling", "reasoning",
        "reasoning_effort", "context_window", "max_output_tokens",
    )
    return ModelCapabilities(**{
        name: bool(value.get(name, False)) for name in names
    })


def _preferred_name(definitions: list[ModelDefinition]) -> str:
    for definition in definitions:
        if definition.provider == "codebuddy":
            return definition.name
    return definitions[0].name
