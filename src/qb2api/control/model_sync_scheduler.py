"""Periodic Qoder upstream model catalog sync (Control-side only)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from qb2api.accounts.qoder_model_sync import sync_qoder_models
from qb2api.config import Settings

logger = logging.getLogger("qb2api.control.model_sync")

_MIN_INTERVAL = 600  # 最短调度间隔 10 分钟


class ModelSyncScheduler:
    """Keep the qoder model catalog fresh from the upstream endpoint.

    Runs one sync immediately at startup, then every ``interval_seconds``.
    A sync that changed the catalog (added/updated/disabled > 0) triggers the
    runtime refresh callback so the Worker picks up the new model list.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        repo: Any,
        registry: Any,
        resolver: Any,
        refresh_callback: Any = None,
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._registry = registry
        self._resolver = resolver
        self._refresh_callback = refresh_callback
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    @property
    def interval_seconds(self) -> int:
        return max(_MIN_INTERVAL, self._settings.model_sync_interval_seconds)

    def set_refresh_callback(self, callback: Any) -> None:
        self._refresh_callback = callback

    def start(self) -> None:
        if not self._settings.model_sync_enabled:
            logger.info("qoder model sync scheduler disabled")
            return
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._loop(), name="qoder-model-sync")
        logger.info("qoder model sync started (interval=%ds)", self.interval_seconds)

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

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.sync_once()
            except Exception:
                logger.warning("qoder model sync cycle failed", exc_info=True)
            delay = max(1.0, float(self.interval_seconds))
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=delay)
                break
            except TimeoutError:
                pass

    async def sync_once(self) -> bool:
        """Run one sync; return True when the catalog changed and refresh ran."""
        report = await sync_qoder_models(
            self._repo,
            self._registry,
            self._resolver,
        )
        changed = (report.added + report.updated + report.disabled) > 0
        if changed:
            logger.info(
                "qoder model sync changed catalog: added=%d updated=%d disabled=%d",
                report.added,
                report.updated,
                report.disabled,
            )
            if self._refresh_callback is not None:
                await self._refresh_callback()
        else:
            logger.info("qoder model sync: no changes")
        return changed
