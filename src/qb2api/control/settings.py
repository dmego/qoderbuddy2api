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
        "monitoring.metrics_interval_seconds": "metrics_interval_seconds",
        "usage.rollup_interval_seconds": "usage_rollup_interval_seconds",
        "usage.detail_retention_days": "usage_detail_retention_days",
    }

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

    @classmethod
    def attribute(cls, key: str) -> str:
        try:
            return cls._ATTRS[key]
        except KeyError as error:
            raise ValueError("setting is not runtime-applicable") from error

    async def apply(self, key: str, value: Any) -> str:
        self.validate(key, value)
        attribute = self.attribute(key)
        old_value = getattr(self.settings, attribute)
        setattr(self.settings, attribute, value)
        try:
            if key.startswith("checkin.") and self.runtime.checkin_scheduler is not None:
                await self.runtime.checkin_scheduler.reconfigure()
            if key == "monitoring.metrics_interval_seconds" and self.runtime.metrics_scheduler:
                self.runtime.metrics_scheduler.reconfigure()
            if key.startswith("usage.") and self.runtime.usage_rollup_service:
                self.runtime.usage_rollup_service.reconfigure()
            return "effective"
        except Exception:
            setattr(self.settings, attribute, old_value)
            raise
