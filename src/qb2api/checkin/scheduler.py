"""In-process check-in scheduler: zoneinfo, catch-up, single task (design §11)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
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


class CheckinScheduler:
    """One asyncio task; shared lock via CheckinService.is_running."""

    def __init__(self, service: CheckinService, settings: Settings) -> None:
        self._service = service
        self._settings = settings
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()
        self._hour, self._minute = parse_checkin_at(settings.checkin_at)
        self._tz = ZoneInfo(settings.checkin_timezone)

    @property
    def next_run_at(self) -> datetime | None:
        if not self._settings.checkin_enabled:
            return None
        return next_run_after(datetime.now(self._tz), self._hour, self._minute)

    def start(self) -> None:
        if not self._settings.checkin_enabled:
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
            self._tz,
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

    async def reconfigure(self) -> None:
        """Apply mutated settings without leaving an old timer alive."""
        await self.stop()
        self._hour, self._minute = parse_checkin_at(self._settings.checkin_at)
        self._tz = ZoneInfo(self._settings.checkin_timezone)
        self.start()

    async def _loop(self) -> None:
        # catch-up once after start
        try:
            await self._maybe_catch_up()
        except Exception as exc:
            logger.warning("checkin catch-up failed: %s", type(exc).__name__)

        while not self._stopped.is_set():
            now = datetime.now(self._tz)
            nxt = next_run_after(now, self._hour, self._minute)
            delay = max(0.5, (nxt - now).total_seconds())
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=delay)
                break
            except TimeoutError:
                pass
            if self._stopped.is_set():
                break
            try:
                await self._service.run_batch(trigger="scheduler", skip_already_done=True)
            except CheckinInProgressError:
                logger.info("scheduled checkin skipped: already running")
            except Exception as exc:
                logger.warning("scheduled checkin failed: %s", type(exc).__name__)

    async def _maybe_catch_up(self) -> None:
        if not self._settings.checkin_catch_up:
            return
        now = datetime.now(self._tz)
        planned = now.replace(
            hour=self._hour, minute=self._minute, second=0, microsecond=0
        )
        if now < planned:
            return
        window = timedelta(hours=self._settings.checkin_catch_up_window_hours)
        if now - planned > window:
            logger.info("checkin catch-up skipped: outside window")
            return
        j = jitter_seconds(
            self._settings.checkin_jitter_min_seconds,
            self._settings.checkin_jitter_max_seconds,
        )
        if j > 0:
            await asyncio.sleep(j)
        if self._stopped.is_set():
            return
        await self._service.run_batch(trigger="catch_up", skip_already_done=True)
