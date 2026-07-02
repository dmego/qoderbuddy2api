"""Model definitions and capabilities."""

import json
import logging
from pathlib import Path
from dataclasses import dataclass, field

logger = logging.getLogger("qb2api")


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

    def to_info(self) -> dict:
        """Convert to /v1/models format."""
        return {
            "id": f"{self.provider}/{self.id}",
            "object": "model",
            "created": 0,
            "owned_by": self.provider,
        }


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
    ModelDefinition("hy3-preview", "Hy3 Preview", "codebuddy", ModelCapabilities()),
]

DEFAULT_QODER_MODELS = [
    ModelDefinition("auto", "Auto", "qoder", ModelCapabilities(tool_calling=True, reasoning_effort=True, context_window=True)),
    ModelDefinition("Qwen3.7-Max", "Qwen 3.7 Max", "qoder", ModelCapabilities(tool_calling=True, reasoning_effort=True, context_window=True)),
    ModelDefinition("Qwen3.7-Plus", "Qwen 3.7 Plus", "qoder", ModelCapabilities(context_window=True)),
    ModelDefinition("Qwen3.6-Flash", "Qwen 3.6 Flash", "qoder", ModelCapabilities(context_window=True)),
    ModelDefinition("DeepSeek-V4-Pro", "DeepSeek V4 Pro", "qoder", ModelCapabilities(tool_calling=True, context_window=True)),
    ModelDefinition("DeepSeek-V4-Flash", "DeepSeek V4 Flash", "qoder", ModelCapabilities(context_window=True)),
    ModelDefinition("GLM-5.2", "GLM-5.2", "qoder", ModelCapabilities(context_window=True)),
    ModelDefinition("Kimi-K2.6", "Kimi K2.6", "qoder", ModelCapabilities(tool_calling=True, context_window=True)),
    ModelDefinition("MiniMax-M2.7", "MiniMax M2.7", "qoder", ModelCapabilities(context_window=True)),
]


def load_models_from_config(config_path: str | Path) -> dict[str, list[ModelDefinition]]:
    """Load model definitions from config file."""
    path = Path(config_path)
    if not path.exists():
        return {
            "codebuddy": DEFAULT_CODEBUDDY_MODELS,
            "qoder": DEFAULT_QODER_MODELS,
        }

    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load model config: {e}, using defaults")
        return {
            "codebuddy": DEFAULT_CODEBUDDY_MODELS,
            "qoder": DEFAULT_QODER_MODELS,
        }

    result = {}
    for provider, provider_data in data.items():
        models = []
        for m in provider_data.get("models", []):
            caps = m.get("capabilities", {})
            capabilities = ModelCapabilities(
                chat=caps.get("chat", True),
                streaming=caps.get("streaming", True),
                tool_calling=caps.get("tool_calling", False),
                reasoning=caps.get("reasoning", False),
                reasoning_effort=caps.get("reasoning_effort", False),
                context_window=caps.get("context_window", False),
                max_output_tokens=caps.get("max_output_tokens", False),
            )
            models.append(ModelDefinition(
                id=m["id"],
                name=m.get("name", m["id"]),
                provider=provider,
                capabilities=capabilities,
                max_context=m.get("max_context", 128000),
                max_output=m.get("max_output", 4096),
            ))
        result[provider] = models

    return result
