"""Serial check-in batch coordination with durable run state."""

from __future__ import annotations

import asyncio
import logging
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
    SUCCESS_VALUES,
    CheckinBatchResult,
    CheckinTarget,
    RunContext,
    batch_view,
    daily_state_view,
    isolated_failure,
    redact_result,
    skipped_terminal,
)
from .codebuddy import WorkBuddyClient
from .executors import CheckinExecutor
from .models import SUCCESS_OUTCOMES, CheckInResult
from .qoder import QoderCheckinClient
from .retry import run_with_retry
from .timing import jitter_seconds as jitter_seconds
from .timing import next_run_after as next_run_after
from .timing import parse_checkin_at as parse_checkin_at

logger = logging.getLogger("qb2api.checkin.service")

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
        self._executor = CheckinExecutor(
            settings=settings,
            repo=repo,
            registry=registry,
            resolver=resolver,
            vault=vault,
            workbuddy=workbuddy,
            qoder=qoder,
        )
        self._running = False
        self._last_run: CheckinBatchResult | None = None
        self._active_context: RunContext | None = None
        self._active_task: asyncio.Task[CheckinBatchResult] | None = None

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

    async def status_snapshot(self, *, next_run_at: str | None = None) -> dict[str, Any]:
        local_date = self.local_date_str()
        timezone = self._settings.checkin_timezone
        states = await self._repo.list_checkin_daily_states(local_date, timezone)
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
                }
                for slot in self._eligible_slots()
            ],
            "daily_states": [daily_state_view(row) for row in states],
            "last_run": batch_view(self._last_run),
        }

    async def run_batch(
        self,
        *,
        trigger: str = "manual",
        targets: list[CheckinTarget] | None = None,
        skip_already_done: bool = True,
    ) -> CheckinBatchResult:
        context = await self._claim_run(trigger)
        return await self._run_context(context, trigger, targets, skip_already_done)

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
            self._run_context(context, trigger, targets, skip_already_done),
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
        context: RunContext,
        trigger: str,
        targets: list[CheckinTarget] | None,
        skip_already_done: bool,
    ) -> CheckinBatchResult:
        try:
            await self._execute_targets(context, trigger, targets, skip_already_done)
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

    async def _execute_targets(
        self,
        context: RunContext,
        trigger: str,
        targets: list[CheckinTarget] | None,
        skip_already_done: bool,
    ) -> None:
        selected = self._resolve_targets(trigger, targets)
        selected.sort(key=lambda item: (item.provider != "codebuddy", item.account_id))
        for target in selected:
            result, attempts = await self._execute_one(
                context,
                target,
                skip_already_done,
            )
            context.results.append(redact_result(result))
            await self._persist_result(context, result, attempts)

    async def _execute_one(
        self,
        context: RunContext,
        target: CheckinTarget,
        skip_already_done: bool,
    ) -> tuple[CheckInResult, int]:
        if skip_already_done and await self._already_terminal(context, target):
            return skipped_terminal(target), 0
        try:
            return await run_with_retry(
                lambda: self._executor.run(target.provider, target.account_id),
                retry_limit=self._settings.checkin_retry_limit,
            )
        except Exception as error:
            logger.warning(
                "checkin account error %s/%s: %s",
                target.provider,
                target.account_id,
                type(error).__name__,
            )
            return isolated_failure(target, error), 1

    def _resolve_targets(
        self,
        trigger: str,
        targets: list[CheckinTarget] | None,
    ) -> list[CheckinTarget]:
        if trigger == "verify":
            return list(targets or [])
        eligible = {
            (slot.provider, slot.account_id)
            for slot in self._eligible_slots()
        }
        if targets is not None:
            return [
                target
                for target in targets
                if (target.provider, target.account_id) in eligible
            ]
        return [CheckinTarget(provider, account_id) for provider, account_id in eligible]

    def _eligible_slots(self) -> list[Any]:
        if not self._settings.checkin_enabled:
            return []
        return [
            slot
            for slot in self._registry.snapshot("checkin")
            if self._provider_enabled(slot.provider)
        ]

    def _provider_enabled(self, provider: str) -> bool:
        if provider == "codebuddy":
            return self._settings.codebuddy_checkin_enabled
        if provider == "qoder":
            return self._settings.qoder_checkin_enabled
        return False

    async def _already_terminal(
        self,
        context: RunContext,
        target: CheckinTarget,
    ) -> bool:
        row = await self._repo.get_checkin_daily_state(
            target.provider,
            target.account_id,
            context.local_date,
            context.timezone,
        )
        return bool(row and row.get("terminal_outcome") in SUCCESS_VALUES)

    async def _persist_result(
        self,
        context: RunContext,
        result: CheckInResult,
        attempts: int,
    ) -> None:
        async with self._repo.transaction():
            await self._repo.upsert_checkin_attempt(
                run_id=context.run_id,
                provider=result.provider,
                account_id=result.account_id,
                outcome=result.outcome.value,
                http_status=result.http_status,
                business_code=(
                    str(result.business_code) if result.business_code is not None else None
                ),
                request_id=result.request_id,
                attempts=attempts,
                redacted_error=None if result.ok else (result.message or result.outcome.value),
            )
            if result.outcome in SUCCESS_OUTCOMES:
                await self._repo.set_checkin_daily_state(
                    provider=result.provider,
                    account_id=result.account_id,
                    local_date=context.local_date,
                    timezone=context.timezone,
                    terminal_outcome=result.outcome.value,
                    last_run_id=context.run_id,
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
