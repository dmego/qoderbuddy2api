"""Growth automation orchestrator — runs after check-in, each step guarded by prerequisites.

Default-on for tasks/lottery/travel/redeem, off for buddy_open. Each step is
independent: a prerequisite miss is a silent skip, not an error. Redeem tier
(default 28d) is configurable via runtime settings.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from qb2api.config import Settings

from .growth import GrowthUnavailableError, WorkBuddyGrowthClient

logger = logging.getLogger("qb2api.checkin.growth_automation")

_REDEEM_DAYS = {"7d": 7, "14d": 14, "28d": 28}


class GrowthAutomation:
    """Run growth-center automation steps for a single codebuddy account."""

    def __init__(
        self,
        settings: Settings,
        client: WorkBuddyGrowthClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or WorkBuddyGrowthClient(
            base_url=settings.codebuddy_checkin_base,
            timeout=float(settings.checkin_request_timeout_seconds),
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def run(self, access_token: str) -> dict[str, Any]:
        """Execute all enabled steps. Returns per-step status; never raises."""
        results: dict[str, str] = {
            "tasks": "skipped", "lottery": "skipped",
            "travel": "skipped", "redeem": "skipped",
            "buddy_open": "skipped",
        }
        if not access_token:
            return {**results, "error": "access_token_missing"}
        if not self._enabled():
            return results
        try:
            overview = await self._client.fetch(access_token)
        except Exception as error:
            return {**results, "error": f"fetch_failed:{type(error).__name__}"}
        if self._settings.growth_auto_tasks:
            results["tasks"] = await self._guard(
                lambda: self._step_tasks(access_token, overview)
            )
        if self._settings.growth_auto_lottery:
            results["lottery"] = await self._guard(
                lambda: self._step_lottery(access_token, overview)
            )
        if self._settings.growth_auto_travel:
            results["travel"] = await self._guard(
                lambda: self._step_travel(access_token)
            )
        if self._settings.growth_auto_redeem:
            results["redeem"] = await self._guard(
                lambda: self._step_redeem(access_token)
            )
        if self._settings.growth_auto_buddy_open:
            results["buddy_open"] = await self._guard(
                lambda: self._step_buddy_open(access_token)
            )
        return results

    def _enabled(self) -> bool:
        return any((
            self._settings.growth_auto_tasks, self._settings.growth_auto_lottery,
            self._settings.growth_auto_travel, self._settings.growth_auto_redeem,
            self._settings.growth_auto_buddy_open,
        ))

    async def _guard(self, operation: Callable[[], Awaitable[str]]) -> str:
        try:
            return await operation()
        except Exception as error:
            logger.warning("growth automation step failed: %s", type(error).__name__)
            return f"failed:{type(error).__name__}"

    async def _step_tasks(self, token: str, overview: dict[str, Any]) -> str:
        tasks = overview.get("tasks", [])
        if not isinstance(tasks, list):
            return "invalid_tasks"
        tasks = [task for task in tasks if isinstance(task, dict)]
        accepted = await self._accept_pending(token, tasks)
        claimed = await self._claim_completed(token, tasks)
        return f"accepted:{accepted} claimed:{claimed}"

    async def _accept_pending(self, token: str, tasks: list[dict[str, Any]]) -> int:
        pending = [t["task_code"] for t in tasks
                    if t.get("accept_status") == "not_accepted"
                    and not t.get("locked") and t.get("task_code")]
        if not pending:
            return 0
        try:
            await self._client.accept_tasks(token, pending)
            return len(pending)
        except GrowthUnavailableError:
            return 0

    async def _claim_completed(self, token: str, tasks: list[dict[str, Any]]) -> int:
        claimed = 0
        for task in tasks:
            if not _task_done(task) or not task.get("has_reward"):
                continue
            code = task.get("task_code")
            if not code:
                continue
            try:
                await self._client.claim_task(token, code)
                claimed += 1
            except GrowthUnavailableError:
                continue
        return claimed

    async def _step_lottery(self, token: str, overview: dict[str, Any]) -> str:
        chances = (overview.get("lottery") or {}).get("available_chances") or 0
        if not chances:
            return "no_chances"
        drawn = 0
        for _ in range(min(chances, 10)):
            try:
                await self._client.lottery_draw(token)
                drawn += 1
            except GrowthUnavailableError:
                break
        return f"drawn:{drawn}/{chances}"

    async def _step_travel(self, token: str) -> str:
        try:
            status = await self._client.travel_status(token)
        except GrowthUnavailableError as error:
            return f"status_failed:{error}"
        state = status.get("state")
        if state == "arrived":
            return await self._claim_travel(token)
        if state == "traveling":
            return "still_traveling"
        return await self._depart_travel(token) if status.get("daily_limit_reached") is False else "daily_limit_reached"

    async def _claim_travel(self, token: str) -> str:
        try:
            await self._client.travel_claim(token)
            return "claimed"
        except GrowthUnavailableError as error:
            return f"claim_failed:{error}"

    async def _depart_travel(self, token: str) -> str:
        try:
            config = await self._client.travel_config(token)
            locations = config.get("locations") or []
            if not locations:
                return "no_locations"
            first = locations[0]
            location_id = first.get("id") if isinstance(first, dict) else None
            if not isinstance(location_id, int):
                return "invalid_location"
            await self._client.travel_depart(token, location_id)
            return "departed"
        except GrowthUnavailableError as error:
            return f"depart_failed:{error}"

    async def _step_redeem(self, token: str) -> str:
        tier = self._settings.growth_redeem_tier
        if tier == "off":
            return "disabled"
        required = _REDEEM_DAYS.get(tier)
        if not required:
            return f"unknown_tier:{tier}"
        try:
            summary = await self._client.redeem_summary(token)
        except GrowthUnavailableError as error:
            return f"summary_failed:{error}"
        remaining = summary.get("remaining_days") or 0
        if remaining < required:
            return f"insufficient:{remaining}/{required}"
        tier_status_key = {"7d": "starter_status", "14d": "advanced_status", "28d": "legendary_status"}[tier]
        if summary.get(tier_status_key) == "unlocked":
            try:
                await self._client.redeem(token, tier)
                return f"redeemed:{tier}"
            except GrowthUnavailableError as error:
                return f"redeem_failed:{error}"
        return f"locked:{tier}"

    async def _step_buddy_open(self, token: str) -> str:
        try:
            quota = await self._client.buddy_quota(token)
        except GrowthUnavailableError as error:
            return f"quota_failed:{error}"
        affordable = quota.get("affordable") or 0
        if not affordable:
            return "not_affordable"
        try:
            await self._client.buddy_open(token, count=min(affordable, quota.get("max_open_count") or 1))
            return f"opened:{affordable}"
        except GrowthUnavailableError as error:
            return f"open_failed:{error}"


def _task_done(task: dict[str, Any]) -> bool:
    current = task.get("progress_current")
    target = task.get("progress_target")
    if isinstance(current, (int, float)) and isinstance(target, (int, float)):
        return current >= target
    return False
