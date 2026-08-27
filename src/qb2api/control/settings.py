"""Runtime setting application with truthful durable status."""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from qb2api.checkin.timing import parse_checkin_at
from qb2api.config import Settings

_RANGE_RULES = {
    "service.worker.start_timeout_seconds": (1, 300, "worker start timeout must be between 1 and 300 seconds"),
    "monitoring.metrics_interval_seconds": (30, 86400, "metrics interval must be between 30 and 86400 seconds"),
    "monitoring.metrics_history_retention_days": (1, 3650, "history retention must be between 1 and 3650 days"),
    "usage.rollup_interval_seconds": (30, 86400, "usage rollup interval must be between 30 and 86400 seconds"),
    "usage.detail_retention_days": (1, 3650, "usage retention must be between 1 and 3650 days"),
    "checkin.catch_up_window_hours": (0, 72, "checkin catch-up window must be between 0 and 72 hours"),
    "checkin.jitter_min_seconds": (0, 300, "checkin jitter must be between 0 and 300 seconds"),
    "checkin.jitter_max_seconds": (0, 300, "checkin jitter must be between 0 and 300 seconds"),
    "checkin.retry_limit": (0, 10, "checkin retry limit must be between 0 and 10"),
}

_REDEEM_TIERS = frozenset({"7d", "14d", "28d", "off"})


def _validate_range(key: str, value: Any) -> None:
    rule = _RANGE_RULES.get(key)
    if rule is None:
        return
    minimum, maximum, message = rule
    if not minimum <= value <= maximum:
        raise ValueError(message)


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
        "monitoring.metrics_history_retention_days": "metrics_history_retention_days",
        "usage.rollup_interval_seconds": "usage_rollup_interval_seconds",
        "usage.detail_retention_days": "usage_detail_retention_days",
        "growth.auto_tasks": "growth_auto_tasks",
        "growth.auto_lottery": "growth_auto_lottery",
        "growth.auto_travel": "growth_auto_travel",
        "growth.auto_redeem": "growth_auto_redeem",
        "growth.redeem_tier": "growth_redeem_tier",
        "growth.auto_buddy_open": "growth_auto_buddy_open",
        "growth.scheduler_enabled": "growth_scheduler_enabled",
        "growth.scheduler_interval_seconds": "growth_scheduler_interval_seconds",
        "growth.auto_active_day": "growth_auto_active_day",
        "growth.auto_active_day_recheckin": "growth_auto_active_day_recheckin",
        "growth.active_day_confirm_attempts": "growth_active_day_confirm_attempts",
        "growth.active_day_max_attempts": "growth_active_day_max_attempts",
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
        cls.attribute(key)
        if key == "checkin.at":
            parse_checkin_at(value)
        if key == "checkin.timezone":
            ZoneInfo(value)
        if key == "growth.redeem_tier" and value not in _REDEEM_TIERS:
            raise ValueError("growth.redeem_tier must be 7d, 14d, 28d, or off")
        if key == "growth.scheduler_interval_seconds" and value < 600:
            raise ValueError("growth.scheduler_interval_seconds must be >= 600")
        _validate_range(key, value)

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
            return await self._apply_runtime_effects(key, value)
        except Exception:
            setattr(self.settings, attribute, old_value)
            raise

    async def _apply_runtime_effects(self, key: str, value: Any) -> dict[str, Any]:
        if key.startswith("monitoring.metrics_") and self.runtime.metrics_scheduler:
            await self.runtime.metrics_scheduler.reconfigure(
                enabled=value if key == "monitoring.metrics_enabled" else None
            )
        if key.startswith("usage.") and self.runtime.usage_rollup_service:
            self.runtime.usage_rollup_service.reconfigure()
        if key.startswith("growth.scheduler_"):
            await self._apply_growth_scheduler(key, value)
        if key == "service.worker.autostart":
            return {"status": "restart_required", "restart_required": True}
        if key == "service.worker.start_timeout_seconds":
            return await self._apply_worker_restart()
        return {"status": "effective", "restart_required": False}

    async def _apply_growth_scheduler(self, key: str, value: Any) -> None:
        scheduler = self.runtime.growth_scheduler
        if scheduler is None:
            return
        if key == "growth.scheduler_enabled":
            await scheduler.reconfigure(enabled=value)
        else:
            await scheduler.reconfigure()

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
