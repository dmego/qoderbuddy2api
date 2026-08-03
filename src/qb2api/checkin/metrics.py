"""Per-account token, quota, points, and check-in metric snapshots."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from qb2api.accounts.registry import AccountRegistry
from qb2api.accounts.repository import AccountRepository
from qb2api.accounts.resolver import CredentialResolver
from qb2api.config import Settings

from .codebuddy_credits import CodeBuddyCreditsClient
from .metrics_collector import MetricDependencies, MetricSnapshotCollector
from .quota import QoderQuotaClient

logger = logging.getLogger("qb2api.checkin.metrics")


class MetricsScheduler:
    """Single-flight scheduler with bounded per-account retry backoff."""

    def __init__(
        self,
        *,
        settings: Settings,
        repo: AccountRepository,
        registry: AccountRegistry,
        resolver: CredentialResolver,
        qoder_quota: QoderQuotaClient | None = None,
        codebuddy_credits: CodeBuddyCreditsClient | None = None,
    ) -> None:
        self.settings = settings
        self.qoder_quota = qoder_quota or QoderQuotaClient(
            base_url=settings.qoder_checkin_base,
            path=settings.qoder_quota_path,
            timeout=float(settings.checkin_request_timeout_seconds),
        )
        self.codebuddy_credits = codebuddy_credits or CodeBuddyCreditsClient(
            base_url=settings.codebuddy_checkin_base,
            path=settings.codebuddy_credits_path,
            timeout=float(settings.checkin_request_timeout_seconds),
        )
        self._task: asyncio.Task[None] | None = None
        self._refresh_task: asyncio.Task[dict[str, Any]] | None = None
        self._lock = asyncio.Lock()
        self._backoff: dict[str, tuple[int, datetime]] = {}
        self._collector = MetricSnapshotCollector(
            MetricDependencies(
                settings=settings,
                repo=repo,
                registry=registry,
                resolver=resolver,
                qoder_quota=self.qoder_quota,
                codebuddy_credits=self.codebuddy_credits,
            ),
            self._backoff,
        )
        self._closed = False
        self._enabled = settings.metrics_enabled
        self._wakeup = asyncio.Event()
        self._last_refresh_at: datetime | None = None
        self._last_error: str | None = None
        self._last_result: dict[str, Any] | None = None

    def start(self) -> None:
        if self._task is None and self._enabled and not self._closed:
            self._task = asyncio.create_task(self._run(), name="qb2api-metrics")

    async def stop(self) -> None:
        self._closed = True
        self._wakeup.set()
        for task in (self._task, self._refresh_task):
            if task is not None and not task.done():
                task.cancel()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
        if self._refresh_task is not None:
            await asyncio.gather(self._refresh_task, return_exceptions=True)
        await self.qoder_quota.aclose()
        await self.codebuddy_credits.aclose()
        self._task = None
        self._refresh_task = None

    async def reconfigure(self, *, enabled: bool | None = None) -> None:
        if enabled is not None and type(enabled) is not bool:
            raise ValueError("metrics enabled must be boolean")
        self._enabled = self.settings.metrics_enabled if enabled is None else enabled
        self._wakeup.set()
        if not self._enabled and self._task is not None:
            task, self._task = self._task, None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        elif self._enabled:
            self.start()

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "running": self._task is not None and not self._task.done(),
            "refresh_in_progress": self._refresh_task is not None and not self._refresh_task.done(),
            "last_refresh_at": self._last_refresh_at.isoformat() if self._last_refresh_at else None,
            "last_error": self._last_error,
            "last_result": self._last_result,
            "backoff": [
                {"metric": key, "attempts": attempts, "retry_at": retry_at.isoformat()}
                for key, (attempts, retry_at) in sorted(self._backoff.items())
            ],
        }

    async def refresh_once(self) -> dict[str, Any]:
        async with self._lock:
            if self._refresh_task is None or self._refresh_task.done():
                self._refresh_task = asyncio.create_task(self._refresh(), name="qb2api-metrics-refresh")
            task = self._refresh_task
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._last_error = type(error).__name__
            raise
        self._last_refresh_at = datetime.now(UTC)
        self._last_error = None
        self._last_result = result
        return result

    async def _run(self) -> None:
        while not self._closed and self._enabled:
            try:
                await self.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("metric refresh failed")
            self._wakeup.clear()
            try:
                await asyncio.wait_for(
                    self._wakeup.wait(),
                    timeout=max(30, self.settings.metrics_interval_seconds),
                )
            except TimeoutError:
                pass

    async def _refresh(self) -> dict[str, Any]:
        return await self._collector.collect()
