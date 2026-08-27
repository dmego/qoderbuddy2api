"""Configuration management."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _env_list(name: str) -> list[str]:
    raw = os.getenv(name)
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


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


def _checkin_flags() -> tuple[bool, bool, bool]:
    codebuddy_enabled = _env_bool("CODEBUDDY_CHECKIN_ENABLED", False)
    qoder_enabled = _env_bool("QODER_CHECKIN_ENABLED", False)
    raw = os.getenv("CHECKIN_ENABLED")
    checkin_enabled = (
        raw.strip().lower() in {"1", "true", "yes", "on"}
        if raw is not None
        else codebuddy_enabled or qoder_enabled
    )
    return codebuddy_enabled, qoder_enabled, checkin_enabled


def _server_values(proxy_api_key: str | None) -> dict[str, object]:
    return {
        "host": os.getenv("QB2API_HOST", "0.0.0.0"),
        "port": _env_int("QB2API_PORT", 9999),
        "control_host": os.getenv("QB2API_CONTROL_HOST", "127.0.0.1"),
        "control_port": _env_int("QB2API_CONTROL_PORT", 9999),
        "worker_host": os.getenv("QB2API_WORKER_HOST", "127.0.0.1"),
        "worker_port": _env_int("QB2API_WORKER_PORT", 10001),
        "worker_start_timeout_seconds": _env_int("QB2API_WORKER_START_TIMEOUT_SECONDS", 30),
        "worker_health_interval_seconds": _env_int("QB2API_WORKER_HEALTH_INTERVAL_SECONDS", 1),
        "worker_shutdown_timeout_seconds": _env_int("QB2API_WORKER_SHUTDOWN_TIMEOUT_SECONDS", 15),
        "worker_autostart": _env_bool("QB2API_WORKER_AUTOSTART", False),
        "worker_internal_token": os.getenv("QB2API_WORKER_INTERNAL_TOKEN") or None,
        "log_level": os.getenv("QB2API_LOG_LEVEL", "info"),
        "proxy_api_key": proxy_api_key,
    }


def _admin_values() -> dict[str, object]:
    return {
        "admin_key": os.getenv("QB2API_ADMIN_KEY") or None,
        "data_dir": os.getenv("QB2API_DATA_DIR", "./data"),
        "credential_key": os.getenv("QB2API_CREDENTIAL_KEY") or None,
        "admin_ui_enabled": _env_bool("QB2API_ADMIN_UI_ENABLED", False),
        "admin_ui_path": os.getenv("QB2API_ADMIN_UI_PATH", "/admin"),
        "admin_cookie_secure": os.getenv("QB2API_ADMIN_COOKIE_SECURE", "auto").strip().lower() or "auto",
        "admin_session_ttl_hours": _env_int("QB2API_ADMIN_SESSION_TTL_HOURS", 12),
        "admin_session_idle_minutes": _env_int("QB2API_ADMIN_SESSION_IDLE_MINUTES", 60),
        "trusted_proxy_headers": _env_bool("QB2API_TRUSTED_PROXY_HEADERS", False),
        "trusted_proxy_networks": _env_list("QB2API_TRUSTED_PROXY_NETWORKS"),
    }


def _provider_values() -> dict[str, object]:
    return {
        "codebuddy_tokens": _parse_tokens(os.getenv("CODEBUDDY_TOKEN")),
        "codebuddy_endpoint": os.getenv("CODEBUDDY_ENDPOINT", "https://copilot.tencent.com"),
        "codebuddy_oauth_enabled": _env_bool("CODEBUDDY_OAUTH_ENABLED", True),
        "codebuddy_oauth_timeout": _env_int("CODEBUDDY_OAUTH_TIMEOUT_SECONDS", 20),
        "codebuddy_oauth_refresh_skew": _env_int("CODEBUDDY_OAUTH_REFRESH_SKEW_SECONDS", 120),
        "qoder_tokens": _parse_tokens(os.getenv("QODER_TOKEN")),
        "qoder_timeout": _env_int("QODER_TIMEOUT", 300),
        "provider_drain_timeout_seconds": _env_int("PROVIDER_DRAIN_TIMEOUT_SECONDS", 330),
        "codebuddy_default_reasoning_effort": os.getenv(
            "QB2API_CODEBUDDY_DEFAULT_REASONING_EFFORT", "low"
        ).strip().lower(),
    }


def _checkin_scheduler_values(enabled: bool) -> dict[str, object]:
    return {
        "checkin_enabled": enabled,
        "checkin_at": os.getenv("CHECKIN_AT", "00:10"),
        "checkin_timezone": os.getenv("CHECKIN_TIMEZONE", "Asia/Shanghai"),
        "checkin_catch_up": _env_bool("CHECKIN_CATCH_UP", True),
        "checkin_catch_up_window_hours": _env_int("CHECKIN_CATCH_UP_WINDOW_HOURS", 6),
        "checkin_jitter_min_seconds": _env_int("CHECKIN_JITTER_MIN_SECONDS", 3),
        "checkin_jitter_max_seconds": _env_int("CHECKIN_JITTER_MAX_SECONDS", 10),
        "checkin_request_timeout_seconds": _env_int("CHECKIN_REQUEST_TIMEOUT_SECONDS", 15),
        "checkin_retry_limit": _env_int("CHECKIN_RETRY_LIMIT", 2),
    }


def _codebuddy_checkin_values(enabled: bool) -> dict[str, object]:
    return {
        "codebuddy_checkin_enabled": enabled,
        "codebuddy_checkin_base": os.getenv("CODEBUDDY_CHECKIN_BASE", "https://www.workbuddy.cn"),
        "codebuddy_checkin_status_path": os.getenv("CODEBUDDY_CHECKIN_STATUS_PATH", "/billing/meter/checkin-status"),
        "codebuddy_checkin_status_method": os.getenv("CODEBUDDY_CHECKIN_STATUS_METHOD", "").strip().upper(),
        "codebuddy_checkin_claim_path": os.getenv("CODEBUDDY_CHECKIN_CLAIM_PATH", "/billing/meter/daily-checkin"),
        "codebuddy_checkin_claim_method": os.getenv("CODEBUDDY_CHECKIN_CLAIM_METHOD", "POST").strip().upper() or "POST",
        "codebuddy_credits_path": os.getenv("CODEBUDDY_CREDITS_PATH", "/billing/meter/get-user-resource"),
    }


def _qoder_checkin_values(enabled: bool) -> dict[str, object]:
    return {
        "qoder_checkin_enabled": enabled,
        "qoder_checkin_base": os.getenv("QODER_CHECKIN_BASE", "https://openapi.qoder.com.cn"),
        "qoder_activity_base": os.getenv("QODER_ACTIVITY_BASE", "https://gateway.qoder.com.cn"),
        "qoder_checkin_status_path": os.getenv("QODER_CHECKIN_STATUS_PATH", "/sash/api/v1/me/daily-check-in/status"),
        "qoder_checkin_claim_path": os.getenv("QODER_CHECKIN_CLAIM_PATH", "/sash/api/v1/me/daily-check-in/claim"),
        "qoder_checkin_refresh_path": os.getenv("QODER_CHECKIN_REFRESH_PATH", "/api/v1/deviceToken/refresh"),
        "qoder_quota_path": os.getenv("QODER_QUOTA_PATH", "/api/v2/quota/usage"),
        "qoder_activity_path": os.getenv("QODER_ACTIVITY_PATH", "/algo/api/v2/activity"),
    }


def _observability_values() -> dict[str, object]:
    return {
        "metrics_enabled": _env_bool("QB2API_METRICS_ENABLED", True),
        "metrics_interval_seconds": _env_int("QB2API_METRICS_INTERVAL_SECONDS", 900),
        "metrics_history_retention_days": _env_int("QB2API_METRICS_HISTORY_RETENTION_DAYS", 90),
        "usage_rollup_interval_seconds": _env_int("QB2API_USAGE_ROLLUP_INTERVAL_SECONDS", 60),
        "usage_detail_retention_days": _env_int("QB2API_USAGE_DETAIL_RETENTION_DAYS", 90),
        "stream_reasoning": _env_bool("QB2API_STREAM_REASONING", True),
        "log_requests": _env_bool("QB2API_LOG_REQUESTS", True),
        "log_dir": os.getenv("QB2API_LOG_DIR", "./logs"),
        "model_config_path": os.getenv("QB2API_MODEL_CONFIG", "./config/models.json"),
        "model_sync_enabled": _env_bool("QB2API_MODEL_SYNC_ENABLED", True),
        "model_sync_interval_seconds": _env_int("QB2API_MODEL_SYNC_INTERVAL_SECONDS", 21600),
        "growth_auto_tasks": _env_bool("GROWTH_AUTO_TASKS", True),
        "growth_auto_lottery": _env_bool("GROWTH_AUTO_LOTTERY", True),
        "growth_auto_travel": _env_bool("GROWTH_AUTO_TRAVEL", True),
        "growth_auto_redeem": _env_bool("GROWTH_AUTO_REDEEM", True),
        "growth_redeem_tier": os.getenv("GROWTH_REDEEM_TIER", "28d"),
        "growth_auto_buddy_open": _env_bool("GROWTH_AUTO_BUDDY_OPEN", False),
        "growth_scheduler_enabled": _env_bool("GROWTH_SCHEDULER_ENABLED", True),
        "growth_scheduler_interval_seconds": _env_int("GROWTH_SCHEDULER_INTERVAL_SECONDS", 1800),
        "growth_auto_active_day_recheckin": _env_bool("GROWTH_AUTO_ACTIVE_DAY_RECHECKIN", True),
        "growth_auto_active_day": _env_bool("GROWTH_AUTO_ACTIVE_DAY", True),
        "growth_active_day_confirm_attempts": _env_int("GROWTH_ACTIVE_DAY_CONFIRM_ATTEMPTS", 3),
    }


@dataclass
class Settings:
    """Application settings loaded from environment variables."""

    # Server
    host: str = "0.0.0.0"
    port: int = 9999
    control_host: str = "127.0.0.1"
    control_port: int = 9999
    worker_host: str = "127.0.0.1"
    worker_port: int = 10001
    worker_start_timeout_seconds: int = 30
    worker_health_interval_seconds: int = 1
    worker_shutdown_timeout_seconds: int = 15
    worker_autostart: bool = False
    worker_internal_token: str | None = None
    log_level: str = "info"

    # Auth — proxy_api_key is canonical; api_key is legacy alias property
    proxy_api_key: str | None = None
    admin_key: str | None = None

    # Storage / vault
    data_dir: str = "./data"
    credential_key: str | None = None

    # Admin UI / session — off by default so legacy env-only proxy still boots
    admin_ui_enabled: bool = False
    admin_ui_path: str = "/admin"
    admin_cookie_secure: str = "auto"  # auto | true | false
    admin_session_ttl_hours: int = 12
    admin_session_idle_minutes: int = 60
    trusted_proxy_headers: bool = False
    trusted_proxy_networks: list[str] = field(default_factory=list)

    # Providers — comma-separated for multiple tokens: CODEBUDDY_TOKEN=key1,key2,key3
    codebuddy_tokens: list[str] = None  # type: ignore
    codebuddy_endpoint: str = "https://copilot.tencent.com"
    codebuddy_oauth_enabled: bool = True
    codebuddy_oauth_timeout: int = 20
    codebuddy_oauth_refresh_skew: int = 120
    qoder_tokens: list[str] = None  # type: ignore
    qoder_timeout: int = 300  # seconds
    provider_drain_timeout_seconds: int = 330

    # Check-in — off by default unless explicitly enabled
    checkin_enabled: bool = False
    checkin_at: str = "00:10"
    checkin_timezone: str = "Asia/Shanghai"
    checkin_catch_up: bool = True
    checkin_catch_up_window_hours: int = 6
    checkin_jitter_min_seconds: int = 3
    checkin_jitter_max_seconds: int = 10
    checkin_request_timeout_seconds: int = 15
    checkin_retry_limit: int = 2

    # CodeBuddy / WorkBuddy check-in (design 13.3)
    codebuddy_checkin_enabled: bool = False
    codebuddy_checkin_base: str = "https://www.workbuddy.cn"
    codebuddy_checkin_status_path: str = "/billing/meter/checkin-status"
    codebuddy_checkin_status_method: str = ""  # empty = no status preflight
    codebuddy_checkin_claim_path: str = "/billing/meter/daily-checkin"
    codebuddy_checkin_claim_method: str = "POST"
    codebuddy_credits_path: str = "/billing/meter/get-user-resource"

    # Qoder check-in (design 13.3)
    qoder_checkin_enabled: bool = False
    qoder_checkin_base: str = "https://openapi.qoder.com.cn"
    qoder_activity_base: str = "https://gateway.qoder.com.cn"
    qoder_checkin_status_path: str = "/sash/api/v1/me/daily-check-in/status"
    qoder_checkin_claim_path: str = "/sash/api/v1/me/daily-check-in/claim"
    qoder_checkin_refresh_path: str = "/api/v1/deviceToken/refresh"
    qoder_quota_path: str = "/api/v2/quota/usage"
    qoder_activity_path: str = "/algo/api/v2/activity"
    metrics_enabled: bool = True
    metrics_interval_seconds: int = 900
    metrics_history_retention_days: int = 90
    usage_rollup_interval_seconds: int = 60
    usage_detail_retention_days: int = 90

    # Streaming — forward reasoning_content (default on for thinking models)
    stream_reasoning: bool = True

    # CodeBuddy/WorkBuddy: inject this reasoning_effort when the client does
    # not specify one, so supported models actually emit reasoning steps.
    # "low" covers all models (hy3 ignores medium); empty disables injection.
    codebuddy_default_reasoning_effort: str = "low"

    # Logging
    log_requests: bool = True
    log_dir: str = "./logs"

    # Model config
    model_config_path: str = "./config/models.json"
    model_sync_enabled: bool = True
    model_sync_interval_seconds: int = 21600  # qoder upstream catalog refresh (6h)

    # Growth automation (WorkBuddy 成长中心自动化)
    growth_auto_tasks: bool = True
    growth_auto_lottery: bool = True
    growth_auto_travel: bool = True
    growth_auto_redeem: bool = True
    growth_redeem_tier: str = "28d"
    growth_auto_buddy_open: bool = False
    growth_scheduler_enabled: bool = True
    growth_scheduler_interval_seconds: int = 1800
    growth_auto_active_day: bool = True
    growth_active_day_confirm_attempts: int = 3
    growth_auto_active_day_recheckin: bool = True

    @property
    def api_key(self) -> str | None:
        """Legacy alias for proxy_api_key (old open proxy auth)."""
        return self.proxy_api_key

    @classmethod
    def from_env(cls, env_file: str | None = None) -> Settings:
        """Load settings from environment variables."""
        load_dotenv(env_file)
        proxy_api_key = os.getenv("QB2API_PROXY_API_KEY") or os.getenv("QB2API_API_KEY")
        codebuddy_enabled, qoder_enabled, checkin_enabled = _checkin_flags()
        values = _server_values(proxy_api_key)
        for section in (
            _admin_values(),
            _provider_values(),
            _checkin_scheduler_values(checkin_enabled),
            _codebuddy_checkin_values(codebuddy_enabled),
            _qoder_checkin_values(qoder_enabled),
            _observability_values(),
        ):
            values.update(section)
        return cls(**values)

    def admin_features_enabled(self) -> bool:
        """True when management UI, check-in, or dynamic credential storage is in use."""
        return bool(
            self.admin_ui_enabled
            or self.checkin_enabled
            or self.codebuddy_checkin_enabled
            or self.qoder_checkin_enabled
            or self.credential_key
        )

    def validate_startup(self) -> None:
        """Reject unsafe or incomplete admin/proxy key configuration."""
        if self.proxy_api_key and self.admin_key and self.proxy_api_key == self.admin_key:
            raise ValueError(
                "QB2API_PROXY_API_KEY and QB2API_ADMIN_KEY must be different"
            )

        if not self.admin_features_enabled():
            return

        if not self.admin_key:
            raise ValueError(
                "QB2API_ADMIN_KEY is required when admin UI, check-in, "
                "or dynamic credentials are enabled"
            )
        if not self.credential_key:
            raise ValueError(
                "QB2API_CREDENTIAL_KEY is required when admin UI, check-in, "
                "or dynamic credentials are enabled"
            )

    def mask_secret(self, value: str | None) -> str:
        """Mask a secret value for safe logging."""
        if not value:
            return "(not set)"
        if len(value) <= 8:
            return "****"
        return f"{value[:4]}...{value[-4:]}"
