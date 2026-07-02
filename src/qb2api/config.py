"""Configuration management."""

import json
import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass
class Settings:
    """Application settings loaded from environment variables."""

    # Server
    host: str = "0.0.0.0"
    port: int = 9999
    log_level: str = "info"

    # API Auth
    api_key: str | None = None

    # Providers — comma-separated for multiple tokens: CODEBUDDY_TOKEN=key1,key2,key3
    codebuddy_tokens: list[str] = None  # type: ignore
    codebuddy_endpoint: str = "https://copilot.tencent.com"
    qoder_tokens: list[str] = None  # type: ignore
    qoder_timeout: int = 300  # seconds

    # Logging
    log_requests: bool = True
    log_dir: str = "./logs"

    # Model config
    model_config_path: str = "./config/models.json"

    @classmethod
    def from_env(cls, env_file: str | None = None) -> "Settings":
        """Load settings from environment variables."""
        load_dotenv(env_file)

        def _parse_tokens(raw: str | None) -> list[str]:
            if not raw:
                return []
            # JSON array (PATCH /api/config serialization)
            if raw.strip().startswith("["):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        return [str(t).strip() for t in parsed if str(t).strip()]
                except json.JSONDecodeError:
                    pass
            return [t.strip() for t in raw.split(",") if t.strip()]

        return cls(
            host=os.getenv("QB2API_HOST", "0.0.0.0"),
            port=int(os.getenv("QB2API_PORT", "9999")),
            log_level=os.getenv("QB2API_LOG_LEVEL", "info"),
            api_key=os.getenv("QB2API_API_KEY"),
            codebuddy_tokens=_parse_tokens(os.getenv("CODEBUDDY_TOKEN")),
            codebuddy_endpoint=os.getenv("CODEBUDDY_ENDPOINT", "https://copilot.tencent.com"),
            qoder_tokens=_parse_tokens(os.getenv("QODER_TOKEN")),
            qoder_timeout=int(os.getenv("QODER_TIMEOUT", "300")),
            log_requests=os.getenv("QB2API_LOG_REQUESTS", "true").lower() == "true",
            log_dir=os.getenv("QB2API_LOG_DIR", "./logs"),
            model_config_path=os.getenv("QB2API_MODEL_CONFIG", "./config/models.json"),
        )

    def mask_secret(self, value: str | None) -> str:
        """Mask a secret value for safe logging."""
        if not value:
            return "(not set)"
        if len(value) <= 8:
            return "****"
        return f"{value[:4]}...{value[-4:]}"
