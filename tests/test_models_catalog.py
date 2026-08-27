"""Tests for the unified model catalog builder."""

from __future__ import annotations

from qb2api.models import ModelCapabilities, ModelDefinition, load_models_from_config
from qb2api.models_catalog import (
    ModelRoute,
    build_unified_catalog,
    normalize_model_id,
)


def _definition(provider: str, model_id: str, **capability_flags) -> ModelDefinition:
    return ModelDefinition(
        id=model_id,
        name=model_id,
        provider=provider,
        capabilities=ModelCapabilities(**capability_flags),
    )


def test_normalize_lowercases_and_strips():
    assert normalize_model_id("  DeepSeek-V4-Flash ") == "deepseek-v4-flash"


def test_shared_model_merges_into_one_dual_route_entry():
    catalog = build_unified_catalog(
        {
            "codebuddy": [_definition("codebuddy", "glm-5.2")],
            "qoder": [_definition("qoder", "GLM-5.2")],
        }
    )
    assert list(catalog) == ["glm-5.2"]
    entry = catalog["glm-5.2"]
    assert entry.routes == (
        ModelRoute("codebuddy", "glm-5.2"),
        ModelRoute("qoder", "GLM-5.2"),
    )
    assert entry.route_for("qoder").upstream_id == "GLM-5.2"


def test_single_provider_models_stay_separate():
    catalog = build_unified_catalog(
        {
            "codebuddy": [_definition("codebuddy", "kimi-k2.7")],
            "qoder": [_definition("qoder", "Kimi-K2.7-Code")],
        }
    )
    assert set(catalog) == {"kimi-k2.7", "kimi-k2.7-code"}
    assert catalog["kimi-k2.7"].routes[0].provider == "codebuddy"
    assert catalog["kimi-k2.7-code"].routes[0].provider == "qoder"


def test_capabilities_are_unioned_and_max_tokens_taken():
    catalog = build_unified_catalog(
        {
            "codebuddy": [
                ModelDefinition(
                    "deepseek-v4-flash", "DeepSeek V4 Flash", "codebuddy",
                    ModelCapabilities(chat=True, streaming=True),
                    max_context=128000, max_output=4096,
                )
            ],
            "qoder": [
                ModelDefinition(
                    "DeepSeek-V4-Flash", "DeepSeek V4 Flash", "qoder",
                    ModelCapabilities(chat=True, streaming=True, context_window=True),
                    max_context=131072, max_output=8192,
                )
            ],
        }
    )
    entry = catalog["deepseek-v4-flash"]
    assert entry.capabilities.context_window is True
    assert entry.capabilities.tool_calling is False
    assert entry.max_context == 131072
    assert entry.max_output == 8192
    assert entry.name == "DeepSeek V4 Flash"  # codebuddy name preferred


def test_override_replaces_routes_and_capabilities():
    catalog = build_unified_catalog(
        {
            "codebuddy": [_definition("codebuddy", "glm-5.2")],
            "qoder": [_definition("qoder", "GLM-5.2")],
        },
        overrides={
            "glm-5.2": {
                "name": "GLM 5.2 (custom)",
                "routes": [{"provider": "codebuddy", "upstream_id": "glm-5.2"}],
                "capabilities": {"chat": True, "streaming": True, "tool_calling": True},
            }
        },
    )
    entry = catalog["glm-5.2"]
    assert entry.name == "GLM 5.2 (custom)"
    assert len(entry.routes) == 1
    assert entry.routes[0].provider == "codebuddy"
    assert entry.capabilities.tool_calling is True
    assert entry.capabilities.reasoning is False


def test_canonicalize_finds_route_by_provider_upstream_pair():
    catalog = build_unified_catalog(
        {
            "codebuddy": [_definition("codebuddy", "deepseek-v4-pro")],
            "qoder": [_definition("qoder", "DeepSeek-V4-Pro")],
        }
    )
    entry = catalog["deepseek-v4-pro"]
    assert entry.canonicalize("qoder", "DeepSeek-V4-Pro") == "deepseek-v4-pro"
    assert entry.canonicalize("qoder", "Other-Id") is None
    assert entry.canonicalize("unknown", "DeepSeek-V4-Pro") is None


def test_to_info_uses_canonical_id_without_prefix():
    catalog = build_unified_catalog(
        {"codebuddy": [_definition("codebuddy", "glm-5.2")]}
    )
    info = catalog["glm-5.2"].to_info()
    assert info["id"] == "glm-5.2"
    assert info["owned_by"] == "qoderbuddy2api"


def test_full_config_only_carries_codebuddy_definitions():
    per_provider = load_models_from_config("config/models.json")
    assert set(per_provider) == {"codebuddy"}
    catalog = build_unified_catalog(per_provider)
    assert len(catalog) == 16
    assert set(catalog["auto"].routes) == {ModelRoute("codebuddy", "auto")}
    assert "glm-5.3" in catalog and "glm-5.3-flash" in catalog


def test_dual_provider_config_merges_to_nineteen_canonical_ids():
    per_provider = load_models_from_config("config/models.json")
    qoder_ids = [
        "auto", "Qwen3.8-Max-Preview", "Qwen3.7-Max", "Qwen3.7-Plus", "Qwen3.6-Flash",
        "DeepSeek-V4-Pro", "DeepSeek-V4-Flash", "GLM-5.2", "Kimi-K2.7-Code", "MiniMax-M2.7",
    ]
    per_provider["qoder"] = [
        ModelDefinition(model_id, model_id, "qoder") for model_id in qoder_ids
    ]
    catalog = build_unified_catalog(per_provider)
    assert len(catalog) == 21
    assert list(catalog) == sorted(catalog)
    assert set(catalog["deepseek-v4-flash"].routes) == {
        ModelRoute("codebuddy", "deepseek-v4-flash"),
        ModelRoute("qoder", "DeepSeek-V4-Flash"),
    }


def test_load_models_ignores_unified_section(tmp_path):
    config_path = tmp_path / "models.json"
    config_path.write_text(
        '{"codebuddy": {"models": [{"id": "hy3", "name": "Hy3"}]}, '
        '"unified": {"hy3": {"name": "Override"}}}'
    )
    loaded = load_models_from_config(config_path)
    assert set(loaded) == {"codebuddy"}
    assert loaded["codebuddy"][0].id == "hy3"
