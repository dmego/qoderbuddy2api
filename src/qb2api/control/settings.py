"""Runtime setting application with truthful durable status."""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from qb2api.checkin.timing import parse_checkin_at
from qb2api.config import Settings


class SettingsApplier:
    """Translate public setting keys into live service mutations."""

    _ATTRS = {
        "service.worker.autostart": "worker_autostart",
        "service.worker.start_timeout_seconds": "worker_start_timeout_seconds",
        "checkin.enabled": "checkin_enabled",
        "checkin.at": "checkin_at",
        "checkin.timezone": "checkin_timezone",
        "checkin.catch_up": "checkin_catch_up",
        "checkin.catch_up_window_hours": "checkin_catch_up_window_hours",
        "checkin.jitter_min_seconds": "checkin_jitter_min_seconds",
        "checkin.jitter_max_seconds": "checkin_jitter_max_seconds",
        "checkin.retry_limit": "checkin_retry_limit",
        "monitoring.metrics_enabled": "metrics_enabled",
        "monitoring.metrics_interval_seconds": "metrics_interval_seconds",
        "usage.rollup_interval_seconds": "usage_rollup_interval_seconds",
        "usage.detail_retention_days": "usage_detail_retention_days",
    }
    _SCHEDULER_KEYS = frozenset(
        {
            "checkin.enabled",
            "checkin.at",
            "checkin.timezone",
            "checkin.catch_up",
            "checkin.catch_up_window_hours",
            "checkin.jitter_min_seconds",
            "checkin.jitter_max_seconds",
        }
    )

    def __init__(self, settings: Settings, runtime: Any) -> None:
        self.settings = settings
        self.runtime = runtime

    @classmethod
    def validate(cls, key: str, value: Any) -> None:
        if key not in cls._ATTRS:
            raise ValueError("setting is not runtime-applicable")
        if key == "checkin.at":
            parse_checkin_at(value)
        elif key == "checkin.timezone":
            ZoneInfo(value)
        elif key == "service.worker.start_timeout_seconds" and not 1 <= value <= 300:
            raise ValueError("worker start timeout must be between 1 and 300 seconds")
        elif key == "monitoring.metrics_interval_seconds" and not 30 <= value <= 86400:
            raise ValueError("metrics interval must be between 30 and 86400 seconds")
        elif key == "usage.rollup_interval_seconds" and not 30 <= value <= 86400:
            raise ValueError("usage rollup interval must be between 30 and 86400 seconds")
        elif key == "usage.detail_retention_days" and not 1 <= value <= 3650:
            raise ValueError("usage retention must be between 1 and 3650 days")
        elif key == "checkin.catch_up_window_hours" and not 0 <= value <= 72:
            raise ValueError("checkin catch-up window must be between 0 and 72 hours")
        elif key in {"checkin.jitter_min_seconds", "checkin.jitter_max_seconds"} and not 0 <= value <= 300:
            raise ValueError("checkin jitter must be between 0 and 300 seconds")
        elif key == "checkin.retry_limit" and not 0 <= value <= 10:
            raise ValueError("checkin retry limit must be between 0 and 10")

    @classmethod
    def attribute(cls, key: str) -> str:
        try:
            return cls._ATTRS[key]
        except KeyError as error:
            raise ValueError("setting is not runtime-applicable") from error

    async def apply(self, key: str, value: Any) -> dict[str, Any]:
        self.validate(key, value)
        attribute = self.attribute(key)
        old_value = getattr(self.settings, attribute)
        try:
            if key in self._SCHEDULER_KEYS:
                return await self._apply_checkin(attribute, value)
            setattr(self.settings, attribute, value)
            if key.startswith("monitoring.metrics_") and self.runtime.metrics_scheduler:
                await self.runtime.metrics_scheduler.reconfigure(
                    enabled=value if key == "monitoring.metrics_enabled" else None
                )
            if key.startswith("usage.") and self.runtime.usage_rollup_service:
                self.runtime.usage_rollup_service.reconfigure()
            if key == "service.worker.autostart":
                return {"status": "restart_required", "restart_required": True}
            if key == "service.worker.start_timeout_seconds":
                return await self._apply_worker_restart()
            return {"status": "effective", "restart_required": False}
        except Exception:
            setattr(self.settings, attribute, old_value)
            raise

    async def _apply_checkin(self, attribute: str, value: Any) -> dict[str, Any]:
        scheduler = self.runtime.checkin_scheduler
        if scheduler is not None:
            await scheduler.reconfigure({attribute: value})
        setattr(self.settings, attribute, value)
        return {"status": "effective", "restart_required": False}

    async def _apply_worker_restart(self) -> dict[str, Any]:
        callback = getattr(self.runtime, "worker_settings_apply", None)
        if callback is None:
            return {"status": "restart_required", "restart_required": True}
        return await callback("restart")
