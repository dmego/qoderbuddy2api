"""Per-target execution and persistence for check-in batches."""

from __future__ import annotations

import logging
from typing import Any

from qb2api.accounts.registry import AccountRegistry
from qb2api.accounts.repository import AccountRepository
from qb2api.config import Settings

from .batch import (
    SUCCESS_VALUES,
    CheckinTarget,
    RunContext,
    isolated_failure,
    redact_result,
    skipped_terminal,
)
from .executors import CheckinExecutor
from .models import SUCCESS_OUTCOMES, CheckInResult
from .retry import run_with_retry

logger = logging.getLogger("qb2api.checkin.service")


class CheckinBatchExecutor:
    """Resolve eligible targets, execute each one, and persist the outcome."""

    def __init__(
        self,
        *,
        settings: Settings,
        repo: AccountRepository,
        registry: AccountRegistry,
        executor: CheckinExecutor,
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._registry = registry
        self._executor = executor

    async def execute(
        self,
        context: RunContext,
        trigger: str,
        targets: list[CheckinTarget] | None,
        skip_already_done: bool,
    ) -> None:
        selected = self.resolve_targets(trigger, targets)
        selected.sort(key=lambda item: (item.provider != "codebuddy", item.account_id))
        for target in selected:
            result, attempts = await self._execute_one(context, target, skip_already_done)
            context.results.append(redact_result(result))
            await self._persist_result(context, result, attempts)

    def resolve_targets(
        self,
        trigger: str,
        targets: list[CheckinTarget] | None,
    ) -> list[CheckinTarget]:
        if trigger == "verify":
            return list(targets or [])
        eligible = {(slot.provider, slot.account_id) for slot in self.eligible_slots()}
        if targets is not None:
            return [
                target
                for target in targets
                if (target.provider, target.account_id) in eligible
            ]
        return [CheckinTarget(provider, account_id) for provider, account_id in eligible]

    def eligible_slots(self) -> list[Any]:
        if not self._settings.checkin_enabled:
            return []
        return [
            slot
            for slot in self._registry.snapshot("checkin")
            if self._provider_enabled(slot.provider)
        ]

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
