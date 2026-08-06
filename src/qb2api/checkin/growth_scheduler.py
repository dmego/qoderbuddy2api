"""Growth 自动化独立调度器，与签到解耦。

定期遍历所有 codebuddy 非环境变量账号，执行已开启的自动化步骤。
调度间隔默认 30 分钟，最小不低于 10 分钟。每个账号独立执行，失败不影响其他账号。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from qb2api.config import Settings

if TYPE_CHECKING:
    from qb2api.accounts.registry import AccountRegistry
    from qb2api.accounts.repository import AccountRepository
    from qb2api.accounts.resolver import CredentialResolver

from .growth_automation import GrowthAutomation

logger = logging.getLogger("qb2api.checkin.growth_scheduler")

_MIN_INTERVAL = 600  # 最小调度间隔 10 分钟


class GrowthScheduler:
    """独立调度 growth 自动化，与签到解耦。"""

    def __init__(
        self,
        *,
        settings: Settings,
        automation: GrowthAutomation,
        registry: AccountRegistry,
        resolver: CredentialResolver,
        repo: AccountRepository,
        metrics_refresh: Any = None,
    ) -> None:
        self._settings = settings
        self._automation = automation
        self._registry = registry
        self._resolver = resolver
        self._repo = repo
        self._metrics_refresh = metrics_refresh
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def interval_seconds(self) -> int:
        return max(_MIN_INTERVAL, self._settings.growth_scheduler_interval_seconds)

    async def reconfigure(self, *, enabled: bool | None = None) -> None:
        """配置变更后重启调度循环（生效新的 interval / enabled）。"""
        if enabled is not None:
            self._settings.growth_scheduler_enabled = enabled
        await self.stop()
        self.start()

    def start(self) -> None:
        if not self._settings.growth_scheduler_enabled:
            logger.info("growth scheduler disabled")
            return
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._loop(), name="growth-scheduler")
        logger.info(
            "growth scheduler started (interval=%ds)", self.interval_seconds,
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
        await self._automation.close()

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await self._run_all_accounts()
            except Exception:
                logger.warning("growth scheduler cycle failed", exc_info=True)
            delay = max(1.0, float(self.interval_seconds))
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=delay)
                break
            except TimeoutError:
                pass

    async def _run_all_accounts(self) -> None:
        slots = [
            slot for slot in self._registry.snapshot("checkin")
            if slot.provider == "codebuddy"
            and not self._registry.is_env_account(slot.provider, slot.account_id)
        ]
        if not slots:
            return
        self._running = True
        try:
            for slot in slots:
                if self._stopped.is_set():
                    break
                await self._run_for_account(slot.provider, slot.account_id)
        finally:
            self._running = False

    async def _run_for_account(self, provider: str, account_id: str) -> None:
        token = await self._resolve_token(provider, account_id)
        if not token:
            logger.info("growth scheduler skip %s/%s: no token", provider, account_id)
            return
        try:
            result = await self._automation.run(token)
        except Exception:
            logger.warning(
                "growth scheduler error %s/%s: %s",
                provider, account_id, type(Exception).__name__, exc_info=True,
            )
            return
        await self._persist_log(provider, account_id, "scheduler", result)
        await self._maybe_refresh_metrics(provider, account_id, result)
        logger.info(
            "growth scheduler completed %s/%s: %s",
            provider, account_id, result,
        )

    async def _resolve_token(self, provider: str, account_id: str) -> str | None:
        for purpose in ("checkin", "chat"):
            try:
                credential = await self._resolver.credential(provider, account_id, purpose)
            except LookupError:
                continue
            token = credential.payload.get("access_token") or credential.payload.get("token")
            if isinstance(token, str) and token.strip():
                return token
        return None

    async def _persist_log(
        self, provider: str, account_id: str,
        triggered_by: str, results: dict,
    ) -> None:
        try:
            await self._repo.insert_growth_log(
                provider=provider, account_id=account_id,
                triggered_by=triggered_by, results=results,
            )
        except Exception:
            logger.warning(
                "growth log persist failed %s/%s",
                provider, account_id, exc_info=True,
            )

    async def _maybe_refresh_metrics(self, provider: str, account_id: str, results: dict) -> None:
        if self._metrics_refresh is None:
            return
        earned = sum(
            (step.get("reward_credits") or 0)
            for step in results.values()
            if isinstance(step, dict)
        )
        if not earned:
            return
        try:
            await self._metrics_refresh()
            logger.info(
                "growth metrics refreshed %s/%s after %d credits earned",
                provider, account_id, earned,
            )
        except Exception:
            logger.warning(
                "growth metrics refresh failed %s/%s",
                provider, account_id, exc_info=True,
            )
