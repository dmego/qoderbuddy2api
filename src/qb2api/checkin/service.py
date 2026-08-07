"""Serial check-in batch coordination with durable run state."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from qb2api.accounts.registry import AccountRegistry
from qb2api.accounts.repository import AccountRepository
from qb2api.accounts.resolver import CredentialResolver
from qb2api.accounts.vault import CredentialVault
from qb2api.config import Settings

from .batch import (
    CheckinBatchResult,
    CheckinTarget,
    RunContext,
    batch_view,
    daily_state_view,
)
from .codebuddy import WorkBuddyClient
from .executors import CheckinExecutor
from .qoder import QoderCheckinClient
from .service_execution import CheckinBatchExecutor
from .timing import jitter_seconds as jitter_seconds
from .timing import next_run_after as next_run_after
from .timing import parse_checkin_at as parse_checkin_at


class CheckinInProgressError(Exception):
    """A scheduler or admin batch already owns the process-wide run slot."""


class CheckinService:
    def __init__(
        self,
        *,
        settings: Settings,
        repo: AccountRepository,
        registry: AccountRegistry,
        resolver: CredentialResolver,
        vault: CredentialVault,
        workbuddy: WorkBuddyClient | None = None,
        qoder: QoderCheckinClient | None = None,
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._registry = registry
        self._resolver = resolver
        self._executor = CheckinExecutor(
            settings=settings,
            repo=repo,
            registry=registry,
            resolver=resolver,
            vault=vault,
            workbuddy=workbuddy,
            qoder=qoder,
        )
        self._batch_executor = CheckinBatchExecutor(
            settings=settings,
            repo=repo,
            registry=registry,
            executor=self._executor,
        )
        self._running = False
        self._last_run: CheckinBatchResult | None = None
        self._active_context: RunContext | None = None
        self._active_task: asyncio.Task[CheckinBatchResult] | None = None

    def set_metrics_refresher(self, callback: Any) -> None:
        self._batch_executor.set_metrics_refresher(callback)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def last_run(self) -> CheckinBatchResult | None:
        return self._last_run

    @property
    def active_run_id(self) -> str | None:
        return self._active_context.run_id if self._active_context is not None else None

    @property
    def qoder_client(self) -> QoderCheckinClient:
        return self._executor.qoder_client

    def local_now(self) -> datetime:
        return datetime.now(ZoneInfo(self._settings.checkin_timezone))

    def local_date_str(self, when: datetime | None = None) -> str:
        return (when or self.local_now()).date().isoformat()

    async def close(self) -> None:
        task = self._active_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._executor.close()

    @staticmethod
    def _credential_token(credential: Any) -> str | None:
        if credential is None:
            return None
        token = credential.payload.get("access_token") or credential.payload.get("token")
        return token if isinstance(token, str) and token.strip() else None

    async def status_snapshot(self, *, next_run_at: str | None = None) -> dict[str, Any]:
        local_date = self.local_date_str()
        timezone = self._settings.checkin_timezone
        states = await self._repo.list_checkin_daily_states(local_date, timezone)
        account_errors = {
            (item.provider, item.account_id): item.purposes.get("checkin", {}).get("last_error")
            for item in self._registry.list_views()
        }
        return {
            "enabled": self._settings.checkin_enabled,
            "running": self._running,
            "local_date": local_date,
            "timezone": timezone,
            "checkin_at": self._settings.checkin_at,
            "catch_up": self._settings.checkin_catch_up,
            "next_run_at": next_run_at,
            "active_run_id": self.active_run_id,
            "eligible_accounts": [
                {
                    "provider": slot.provider,
                    "account_id": slot.account_id,
                    "status": slot.status,
                    "verification_status": slot.verification_status,
                    "last_error": account_errors.get((slot.provider, slot.account_id)),
                }
                for slot in self._batch_executor.eligible_slots()
            ],
            "daily_states": [daily_state_view(row) for row in states],
            "last_run": batch_view(self._last_run),
        }

    async def has_pending_targets(self) -> bool:
        """Return whether at least one enabled account still needs today's check-in."""
        targets = self._batch_executor.resolve_targets("catch_up", None)
        if not targets:
            return False
        local_date = self.local_date_str()
        timezone = self._settings.checkin_timezone
        for target in targets:
            state = await self._repo.get_checkin_daily_state(
                target.provider, target.account_id, local_date, timezone
            )
            terminal = state.get("terminal_outcome") if state else None
            if terminal not in {"CLAIMED", "ALREADY_CHECKED_IN"}:
                return True
        return False

    async def run_batch(
        self,
        *,
        trigger: str = "manual",
        targets: list[CheckinTarget] | None = None,
        skip_already_done: bool = True,
    ) -> CheckinBatchResult:
        context = await self._claim_run(trigger)
        return await self._run_context(
            context=context,
            trigger=trigger,
            targets=targets,
            skip_already_done=skip_already_done,
        )

    async def start_batch(
        self,
        *,
        trigger: str = "manual",
        targets: list[CheckinTarget] | None = None,
        skip_already_done: bool = True,
    ) -> str:
        """Persist a batch before returning so a caller can poll its run ID."""
        context = await self._claim_run(trigger)
        task = asyncio.create_task(
            self._run_context(
                context=context,
                trigger=trigger,
                targets=targets,
                skip_already_done=skip_already_done,
            ),
            name=f"qb2api-checkin-{context.run_id}",
        )
        self._active_task = task
        return context.run_id

    async def _claim_run(self, trigger: str) -> RunContext:
        if self._running:
            raise CheckinInProgressError("checkin_run_in_progress")
        self._running = True
        context = RunContext(
            run_id=uuid.uuid4().hex,
            local_date=self.local_date_str(),
            timezone=self._settings.checkin_timezone,
        )
        self._active_context = context
        try:
            await self._create_run(context, trigger)
        except BaseException:
            self._active_context = None
            self._running = False
            raise
        return context

    async def _run_context(
        self,
        *,
        context: RunContext,
        trigger: str,
        targets: list[CheckinTarget] | None,
        skip_already_done: bool,
    ) -> CheckinBatchResult:
        try:
            await self._batch_executor.execute(
                context=context,
                trigger=trigger,
                targets=targets,
                skip_already_done=skip_already_done,
            )
            return await self._finish(context, "finished")
        except asyncio.CancelledError:
            await asyncio.shield(self._finish(context, "cancelled"))
            raise
        except Exception as error:
            await self._finish(context, "failed", type(error).__name__)
            raise
        finally:
            self._active_context = None
            if self._active_task is asyncio.current_task():
                self._active_task = None
            self._running = False

    async def _create_run(self, context: RunContext, trigger: str) -> None:
        await self._repo.create_checkin_run(
            run_id=context.run_id,
            local_date=context.local_date,
            timezone=context.timezone,
            trigger=trigger,
        )


    async def _finish(
        self,
        context: RunContext,
        status: str,
        error: str | None = None,
    ) -> CheckinBatchResult:
        await self._repo.finish_checkin_run(
            context.run_id,
            status=status,
            error_message=error,
        )
        batch = CheckinBatchResult(
            run_id=context.run_id,
            local_date=context.local_date,
            timezone=context.timezone,
            status=status,
            results=context.results,
        )
        self._last_run = batch
        return batch
