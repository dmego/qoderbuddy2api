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
        )
    ]


def _matches_model(
    model: dict[str, Any],
    *,
    enabled: bool | None,
    source: str | None,
    capability: str | None,
    needle: str | None,
) -> bool:
    if enabled is not None and model["enabled"] is not enabled:
        return False
    if source is not None and model["source"] != source:
        return False
    if capability is not None and capability not in model["capabilities"]:
        return False
    return needle is None or _matches_model_search(model, needle)


def _matches_model_search(model: dict[str, Any], needle: str) -> bool:
    return needle in model["model_id"].casefold() or needle in model["display_name"].casefold()
