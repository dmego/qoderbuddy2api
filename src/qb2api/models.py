"""Model definitions and capabilities."""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("qb2api")

KNOWN_PROVIDERS = ("codebuddy", "qoder", "workbuddy_intl")

# Providers whose model definitions come from the config file. Qoder's catalog
# is owned by its upstream sync instead, so it is loaded separately.
CONFIG_PROVIDERS = ("codebuddy", "workbuddy_intl")

# The international WorkBuddy deployment has no sign-in or growth centre, so its
# accounts carry a chat purpose only and must never accrue check-in state.
CHAT_ONLY_PROVIDERS = frozenset({"workbuddy_intl"})


@dataclass
class ModelCapabilities:
    """Model capabilities."""
    chat: bool = True
    streaming: bool = True
    tool_calling: bool = False
    reasoning: bool = False
    reasoning_effort: bool = False
    context_window: bool = False
    max_output_tokens: bool = False


@dataclass
class ModelDefinition:
    """Model definition with metadata."""
    id: str
    name: str
    provider: str
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    max_context: int = 128000
    max_output: int = 4096
    metadata: dict | None = None


# Default model definitions (used when config file is missing)
DEFAULT_CODEBUDDY_MODELS = [
    ModelDefinition("auto", "Auto", "codebuddy", ModelCapabilities()),
    ModelDefinition("deepseek-v3", "DeepSeek V3", "codebuddy", ModelCapabilities(tool_calling=True)),
    ModelDefinition("deepseek-v3-0324", "DeepSeek V3 (0324)", "codebuddy", ModelCapabilities()),
    ModelDefinition("deepseek-v4-pro", "DeepSeek V4 Pro", "codebuddy", ModelCapabilities()),
    ModelDefinition("deepseek-v4-flash", "DeepSeek V4 Flash", "codebuddy", ModelCapabilities()),
    ModelDefinition("deepseek-r1", "DeepSeek R1", "codebuddy", ModelCapabilities(reasoning=True)),
    ModelDefinition("glm-5.1", "GLM-5.1", "codebuddy", ModelCapabilities()),
    ModelDefinition("glm-5.2", "GLM-5.2", "codebuddy", ModelCapabilities()),
    ModelDefinition("glm-5v-turbo", "GLM-5v-Turbo", "codebuddy", ModelCapabilities()),
    ModelDefinition("minimax-m3", "MiniMax M3", "codebuddy", ModelCapabilities()),
    ModelDefinition("minimax-m2.7", "MiniMax M2.7", "codebuddy", ModelCapabilities(reasoning=True)),
    ModelDefinition("kimi-k2.6", "Kimi K2.6", "codebuddy", ModelCapabilities()),
    ModelDefinition("kimi-k2.7", "Kimi K2.7", "codebuddy", ModelCapabilities(reasoning=True)),
    ModelDefinition("hy3", "Hy3", "codebuddy", ModelCapabilities()),
]

# International WorkBuddy (www.workbuddy.ai) free tier. Only these three ids are
# usable there without spending credits; probing other ids is not permitted.
DEFAULT_INTL_MODELS = [
    ModelDefinition("hy4-preview", "Hy4 Preview", "workbuddy_intl", ModelCapabilities(
        reasoning=True, reasoning_effort=True, tool_calling=True,
    )),
    ModelDefinition("hy3", "Hy3", "workbuddy_intl", ModelCapabilities(
        reasoning=True, reasoning_effort=True, tool_calling=True,
    )),
    ModelDefinition("deepseek-v4.1-flash", "DeepSeek V4.1 Flash", "workbuddy_intl", ModelCapabilities(
        reasoning=True, reasoning_effort=True, tool_calling=True,
    )),
]


def load_models_from_config(config_path: str | Path) -> dict[str, list[ModelDefinition]]:
    """Load model definitions from config file (known providers only)."""
    path = Path(config_path)
    if not path.exists():
        return {"codebuddy": DEFAULT_CODEBUDDY_MODELS, "workbuddy_intl": DEFAULT_INTL_MODELS}

    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load model config: {e}, using defaults")
        return {"codebuddy": DEFAULT_CODEBUDDY_MODELS, "workbuddy_intl": DEFAULT_INTL_MODELS}

    result = {}
    for provider in CONFIG_PROVIDERS:
        provider_data = data.get(provider)
        defaults = _defaults_for(provider)
        if not isinstance(provider_data, dict):
            if defaults:
                result[provider] = list(defaults)
            continue
        result[provider] = [
            _model_definition(provider, m) for m in provider_data.get("models", [])
        ] or list(defaults)

    return result


def _defaults_for(provider: str) -> list[ModelDefinition]:
    if provider == "workbuddy_intl":
        return DEFAULT_INTL_MODELS
    if provider == "codebuddy":
        return DEFAULT_CODEBUDDY_MODELS
    return []


def _model_definition(provider: str, raw: dict) -> ModelDefinition:
    caps = raw.get("capabilities", {})
    capabilities = ModelCapabilities(
        chat=caps.get("chat", True),
        streaming=caps.get("streaming", True),
        tool_calling=caps.get("tool_calling", False),
        reasoning=caps.get("reasoning", False),
        reasoning_effort=caps.get("reasoning_effort", False),
        context_window=caps.get("context_window", False),
        max_output_tokens=caps.get("max_output_tokens", False),
    )
    return ModelDefinition(
        id=raw["id"],
        name=raw.get("name", raw["id"]),
        provider=provider,
        capabilities=capabilities,
        max_context=raw.get("max_context", 128000),
        max_output=raw.get("max_output", 4096),
    )


def load_unified_overrides(config_path: str | Path) -> dict:
    """Load the optional top-level ``unified`` section of the model config."""
    path = Path(config_path)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load unified model overrides: {e}")
        return {}
    section = data.get("unified")
    return section if isinstance(section, dict) else {}
