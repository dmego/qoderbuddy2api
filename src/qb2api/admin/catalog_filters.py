"""Model-catalog filtering helpers."""

from __future__ import annotations

from typing import Any


def filter_models(
    models: list[dict[str, Any]],
    *,
    enabled: bool | None,
    source: str | None,
    capability: str | None,
    search: str | None,
    provider: str | None = None,
) -> list[dict[str, Any]]:
    needle = search.casefold() if search is not None else None
    return [
        model
        for model in models
        if _matches_model(
            model,
            enabled=enabled,
            source=source,
            capability=capability,
            needle=needle,
            provider=provider,
        )
    ]


def _matches_model(
    model: dict[str, Any],
    *,
    enabled: bool | None,
    source: str | None,
    capability: str | None,
    needle: str | None,
    provider: str | None,
) -> bool:
    if enabled is not None and model["enabled"] is not enabled:
        return False
    if source is not None and model["source"] != source:
        return False
    if capability is not None and capability not in model["capabilities"]:
        return False
    if provider is not None and not _has_route_provider(model, provider):
        return False
    return needle is None or _matches_model_search(model, needle)


def _has_route_provider(model: dict[str, Any], provider: str) -> bool:
    return any(route["provider"] == provider for route in model.get("routes", []))


def _matches_model_search(model: dict[str, Any], needle: str) -> bool:
    return needle in model["model_id"].casefold() or needle in model["display_name"].casefold()
