"""Per-target execution and persistence for check-in batches."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
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
from .models import SUCCESS_OUTCOMES, CheckInOutcome, CheckInResult
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
        metrics_refresh: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._registry = registry
        self._executor = executor
        self._metrics_refresh = metrics_refresh

    def set_metrics_refresher(self, callback: Callable[[], Awaitable[Any]] | None) -> None:
        self._metrics_refresh = callback

    async def execute(
        self,
        *,
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
        before = await self._snapshot(target)
        try:
            result, attempts = await run_with_retry(
                lambda: self._executor.run(target.provider, target.account_id),
                retry_limit=self._settings.checkin_retry_limit,
            )
            if result.outcome in SUCCESS_OUTCOMES:
                result = await self._observe_quota(target, result, before)
            return result, attempts
        except Exception as error:
            logger.warning(
                "checkin account error %s/%s: %s",
                target.provider,
                target.account_id,
                type(error).__name__,
            )
            return isolated_failure(target, error), 1

    async def _observe_quota(
        self, target: CheckinTarget, result: CheckInResult, before: dict[str, Any] | None
    ) -> CheckInResult:
        if self._metrics_refresh is None:
            result.quota_before = before
            result.quota_change_status = "claimed_balance_pending"
            return result
        try:
            await self._metrics_refresh()
            after = await self._snapshot(target)
        except Exception as error:
            logger.warning("quota refresh failed %s/%s: %s", target.provider, target.account_id, type(error).__name__)
            result.quota_before = before
            result.quota_change_status = "claimed_balance_pending"
            return result
        result.quota_before = before
        result.quota_after = after
        _append_reward_package(after, result)
        result.quota_delta = _quota_delta(before, after)
        result.quota_observed_at = after.get("observed_at") if after else None
        result.quota_change_status = _quota_change_status(result)
        return result

    async def _snapshot(self, target: CheckinTarget) -> dict[str, Any] | None:
        rows = await self._repo.list_metric_snapshots(target.provider, target.account_id)
        row = next((item for item in rows if item.get("metric_kind") in {"quota", "points"}), None)
        if row is None or row.get("status") != "fresh" or not isinstance(row.get("value"), dict):
            return None
        value = row["value"]
        packages = value.get("packages") if isinstance(value.get("packages"), list) else _quota_packages(value)
        return {"packages": packages, "observed_at": row.get("observed_at"), "status": row.get("status")}

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
                reward_credits=result.reward_credits,
                reward_expires_at=result.reward_expires_at,
                quota_before=result.quota_before,
                quota_after=result.quota_after,
                quota_delta=result.quota_delta,
                quota_observed_at=result.quota_observed_at,
                quota_change_status=result.quota_change_status,
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


def _quota_packages(value: dict[str, Any]) -> list[dict[str, Any]]:
    packages: list[dict[str, Any]] = []
    for name, detail in (("user_quota", value.get("user_quota")), ("add_on_quota", value.get("add_on_quota"))):
        if isinstance(detail, dict):
            package = {"name": name}
            package.update({key: detail[key] for key in ("remaining", "total", "unit", "expires_at") if key in detail})
            packages.append(package)
    if packages:
        return packages
    if isinstance(value.get("total_remaining"), (int, float)):
        return [{"name": "total", "remaining": value["total_remaining"]}]
    return []


def _append_reward_package(snapshot: dict[str, Any] | None, result: CheckInResult) -> None:
    if not snapshot or result.provider != "qoder" or result.reward_credits is None:
        return
    packages = snapshot.setdefault("packages", [])
    if not isinstance(packages, list):
        return
    package: dict[str, Any] = {
        "name": "签到奖励",
        "remaining": result.reward_credits,
        "total": result.reward_credits,
        "unit": "credits",
    }
    if result.reward_expires_at:
        package["expires_at"] = result.reward_expires_at
    packages.append(package)


def _quota_delta(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any] | None:
    if not before or not after:
        return None
    old = _package_map(before)
    new = _package_map(after)
    packages = [_package_delta(name, old.get(name), new.get(name)) for name in sorted(set(old) | set(new))]
    packages = [item for item in packages if item is not None]
    return {"packages": packages} if packages else None


def _package_map(snapshot: dict[str, Any]) -> dict[Any, dict[str, Any]]:
    return {item.get("name"): item for item in snapshot.get("packages", []) if isinstance(item, dict)}


def _package_delta(name: Any, previous: dict[str, Any] | None, current: dict[str, Any] | None) -> dict[str, Any] | None:
    if not previous or not current:
        return None
    old_remaining, new_remaining = previous.get("remaining"), current.get("remaining")
    if not isinstance(old_remaining, (int, float)) or not isinstance(new_remaining, (int, float)):
        return None
    return {"name": name, "delta": new_remaining - old_remaining}


def _quota_change_status(result: CheckInResult) -> str:
    if result.outcome == CheckInOutcome.CLAIMED and result.quota_delta:
        if any(item.get("delta", 0) > 0 for item in result.quota_delta.get("packages", [])):
            return "claimed_balance_increased"
        return "claimed_balance_unchanged"
    if result.outcome == CheckInOutcome.ALREADY_CHECKED_IN:
        return "already_checked_in"
    return "claimed_balance_pending"
