"""In-process check-in scheduler: zoneinfo, catch-up, single task (design §11)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from qb2api.checkin.service import (
    CheckinInProgressError,
    CheckinService,
    jitter_seconds,
    next_run_after,
    parse_checkin_at,
)
from qb2api.config import Settings

logger = logging.getLogger("qb2api.checkin.scheduler")


@dataclass(frozen=True)
class ScheduleConfiguration:
    enabled: bool
    hour: int
    minute: int
    timezone: ZoneInfo
    catch_up: bool
    catch_up_window_hours: int
    jitter_min_seconds: int
    jitter_max_seconds: int

    @classmethod
    def from_settings(
        cls, settings: Settings, overrides: dict[str, Any] | None = None
    ) -> ScheduleConfiguration:
        values = overrides or {}
        checkin_at = str(values.get("checkin_at", settings.checkin_at))
        timezone = str(values.get("checkin_timezone", settings.checkin_timezone))
        hour, minute = parse_checkin_at(checkin_at)
        jitter_min = int(values.get("checkin_jitter_min_seconds", settings.checkin_jitter_min_seconds))
        jitter_max = int(values.get("checkin_jitter_max_seconds", settings.checkin_jitter_max_seconds))
        if jitter_min < 0 or jitter_max < jitter_min:
            raise ValueError("invalid checkin jitter range")
        window = int(values.get("checkin_catch_up_window_hours", settings.checkin_catch_up_window_hours))
        if window < 0:
            raise ValueError("invalid checkin catch-up window")
        return cls(
            enabled=bool(values.get("checkin_enabled", settings.checkin_enabled)),
            hour=hour, minute=minute, timezone=ZoneInfo(timezone),
            catch_up=bool(values.get("checkin_catch_up", settings.checkin_catch_up)),
            catch_up_window_hours=window, jitter_min_seconds=jitter_min, jitter_max_seconds=jitter_max,
        )


class CheckinScheduler:
    """One asyncio task; shared lock via CheckinService.is_running."""

    def __init__(self, service: CheckinService, settings: Settings) -> None:
        self._service = service
        self._settings = settings
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()
        self._configuration = ScheduleConfiguration.from_settings(settings)
        self._reconfigure_lock = asyncio.Lock()
        self._catch_up_decision = "not_evaluated"
        self._last_error: str | None = None
        self._last_run_at: datetime | None = None

    @property
    def next_run_at(self) -> datetime | None:
        if not self._configuration.enabled:
            return None
        return next_run_after(
            datetime.now(self._configuration.timezone),
            self._configuration.hour,
            self._configuration.minute,
        )

    def status_snapshot(self) -> dict[str, Any]:
        next_run = self.next_run_at
        return {
            "next_run_at": next_run.isoformat() if next_run else None,
            "catch_up_decision": self._catch_up_decision,
            "active_run_id": self._service.active_run_id,
            "last_error": self._last_error,
            "last_run_at": self._last_run_at.isoformat() if self._last_run_at else None,
        }

    def start(self) -> None:
        if not self._configuration.enabled:
            logger.info("checkin scheduler disabled")
            return
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._loop(), name="checkin-scheduler")
        logger.info(
            "checkin scheduler started at %s %s (tz=%s)",
            self._settings.checkin_at,
            self._settings.checkin_timezone,
            self._configuration.timezone,
        )

    async def stop(self) -> None:
        self._stopped.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def reconfigure(self, overrides: dict[str, Any] | None = None) -> None:
        """Validate the replacement schedule before cancelling the old timer."""
        candidate = ScheduleConfiguration.from_settings(self._settings, overrides)
        async with self._reconfigure_lock:
            await self.stop()
            self._configuration = candidate
            self._catch_up_decision = "not_evaluated"
            self._last_error = None
            self.start()

    async def _loop(self) -> None:
        try:
            await self._maybe_catch_up()
        except Exception as exc:
            self._last_error = type(exc).__name__
            logger.warning("checkin catch-up failed: %s", type(exc).__name__)

        while not self._stopped.is_set():
            now = datetime.now(self._configuration.timezone)
            nxt = next_run_after(now, self._configuration.hour, self._configuration.minute)
            delay = max(0.5, (nxt - now).total_seconds())
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=delay)
                break
            except TimeoutError:
                pass
            if self._stopped.is_set():
                break
            try:
                await self._launch_batch("scheduler")
            except CheckinInProgressError:
                self._last_error = "checkin_run_in_progress"
                logger.info("scheduled checkin skipped: already running")
            except Exception as exc:
                self._last_error = type(exc).__name__
                logger.warning("scheduled checkin failed: %s", type(exc).__name__)

    async def _maybe_catch_up(self) -> None:
        configuration = self._configuration
        if not configuration.catch_up:
            self._catch_up_decision = "disabled"
            return
        now = datetime.now(configuration.timezone)
        planned = now.replace(
            hour=configuration.hour, minute=configuration.minute, second=0, microsecond=0
        )
        if now < planned:
            self._catch_up_decision = "before_schedule"
            return
        window = timedelta(hours=configuration.catch_up_window_hours)
        if now - planned > window:
            self._catch_up_decision = "outside_window"
            logger.info("checkin catch-up skipped: outside window")
            return
        if not await self._has_pending_targets():
            self._catch_up_decision = "already_complete"
            logger.info("checkin catch-up skipped: all targets already terminal")
            return
        j = jitter_seconds(
            configuration.jitter_min_seconds,
            configuration.jitter_max_seconds,
        )
        if j > 0:
            await asyncio.sleep(j)
        if self._stopped.is_set():
            self._catch_up_decision = "stopped"
            return
        await self._launch_batch("catch_up")
        self._catch_up_decision = "started"

    async def _has_pending_targets(self) -> bool:
        checker = getattr(self._service, "has_pending_targets", None)
        if checker is None:
            return True
        return bool(await checker())

    async def _launch_batch(self, trigger: str) -> None:
        start_batch = getattr(self._service, "start_batch", None)
        if start_batch is None:
            await self._service.run_batch(trigger=trigger, skip_already_done=True)
        else:
            await start_batch(trigger=trigger, skip_already_done=True)
        self._last_run_at = datetime.now(self._configuration.timezone)
