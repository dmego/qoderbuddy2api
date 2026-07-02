"""Configuration management."""

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

    # Cache
    cache_enabled: bool = True
    cache_max_size: int = 200
    cache_ttl: int = 300  # seconds

    # Model config
    model_config_path: str = "./config/models.json"

    @classmethod
    def from_env(cls, env_file: str | None = None) -> "Settings":
        """Load settings from environment variables."""
        load_dotenv(env_file)

        def _parse_tokens(raw: str | None) -> list[str]:
            if not raw:
                return []
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
            cache_enabled=os.getenv("QB2API_CACHE_ENABLED", "true").lower() == "true",
            cache_max_size=int(os.getenv("QB2API_CACHE_MAX_SIZE", "200")),
            cache_ttl=int(os.getenv("QB2API_CACHE_TTL", "300")),
            model_config_path=os.getenv("QB2API_MODEL_CONFIG", "./config/models.json"),
        )

    def mask_secret(self, value: str | None) -> str:
        """Mask a secret value for safe logging."""
        if not value:
            return "(not set)"
        if len(value) <= 8:
            return "****"
        return f"{value[:4]}...{value[-4:]}"
