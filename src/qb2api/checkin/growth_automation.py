"""Growth automation orchestrator - independent steps, structured results.

Each step returns a structured dict with status + detail + optional metrics.
Steps are independent: a prerequisite miss is a silent skip, not an error.
Redeem tier (default 28d) is configurable via runtime settings.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from qb2api.accounts.repository import AccountRepository
from qb2api.config import Settings

from .active_day import ActiveDayError, WorkBuddyActiveDayClient
from .growth import GrowthUnavailableError, WorkBuddyGrowthClient
from .models import CheckInOutcome

logger = logging.getLogger("qb2api.checkin.growth_automation")

_REDEEM_DAYS = {"7d": 7, "14d": 14, "28d": 28}
_STEP_KEYS = ("tasks", "lottery", "travel", "redeem", "buddy_open", "active_day")


def _today_lit(overview: dict[str, Any], local_date: str) -> bool:
    """成长中心当天(local_date)是否已被上游记账点亮（格子 score>0）。

    只以热力图格子里该日期的 score 为准；heatmap.today 只有在它携带的日期与本地
    日期一致时才可信——官方 today 按上游日界线计算，凌晨(本地 00:00-08:00)会把
    "UTC 昨日已点亮"误判为本地当天已点亮，导致漏跑 ACP / 误标 confirmed。
    """
    heatmap = overview.get("heatmap") or {}
    for cell in heatmap.get("cells") or []:
        if isinstance(cell, dict) and cell.get("date") == local_date:
            score = cell.get("score")
            return isinstance(score, (int, float)) and score > 0
    today = heatmap.get("today") or {}
    if today.get("date") == local_date:
        if today.get("is_active") is True:
            return True
        score = today.get("score")
        return isinstance(score, (int, float)) and score > 0
    return False


class GrowthAutomation:
    """Run growth-center automation steps for a single codebuddy account."""

    def __init__(
        self,
        settings: Settings,
        client: WorkBuddyGrowthClient | None = None,
        *,
        repository: AccountRepository | None = None,
        active_day_client: WorkBuddyActiveDayClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or WorkBuddyGrowthClient(
            base_url=settings.codebuddy_checkin_base,
            timeout=float(settings.checkin_request_timeout_seconds),
        )
        self._owns_client = client is None
        self._repository = repository
        self._active_day_client = active_day_client
        self._owns_active_day_client = active_day_client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
        if self._owns_active_day_client and self._active_day_client is not None:
            await self._active_day_client.aclose()

    async def run_active_day(
        self,
        access_token: str,
        *,
        account_id: str,
        local_date: str,
        timezone: str,
        overview: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        # 开关由调用方(run 的 growth_auto_active_day)决定；手动单步执行不在此拦截。
        if self._repository is None:
            return {"status": "repository_missing"}
        if not access_token:
            return {"status": "access_token_missing"}
        if self._active_day_client is None:
            self._active_day_client = WorkBuddyActiveDayClient(
                base_url=self._settings.codebuddy_endpoint,
                timeout=float(self._settings.checkin_request_timeout_seconds),
            )
        claimed = await self._repository.claim_workbuddy_active_day(
            provider="codebuddy", account_id=account_id, local_date=local_date, timezone=timezone
        )
        if not claimed:
            return {"status": "already_claimed"}
        if (
            not (overview is not None and _today_lit(overview, local_date))
            and self._settings.growth_auto_active_day_recheckin
        ):
            try:
                if await self._recheckin_unlit(access_token):
                    overview = None  # 签到成功后重新拉取成长中心
            except Exception as error:
                logger.info("active-day recheckin skipped %s: %s", account_id, type(error).__name__)
        if overview is None:
            try:
                overview = await self._client.fetch(access_token)
            except Exception:
                overview = None
        if overview is not None and _today_lit(overview, local_date):
            await self._repository.finish_workbuddy_active_day(
                provider="codebuddy", account_id=account_id, local_date=local_date, timezone=timezone,
                status="skipped_external", confirmed="lit",
            )
            return {"status": "skipped_external"}
        try:
            await self._active_day_client.run(access_token)
        except ActiveDayError as error:
            await self._repository.finish_workbuddy_active_day(
                provider="codebuddy", account_id=account_id, local_date=local_date, timezone=timezone,
                status="failed", error_code=error.code,
            )
            return {"status": "failed", "error_code": error.code}
        except Exception as error:
            await self._repository.finish_workbuddy_active_day(
                provider="codebuddy", account_id=account_id, local_date=local_date, timezone=timezone,
                status="failed", error_code=type(error).__name__,
            )
            return {"status": "failed", "error_code": type(error).__name__}
        await self._repository.finish_workbuddy_active_day(
            provider="codebuddy", account_id=account_id, local_date=local_date, timezone=timezone,
            status="succeeded",
        )
        return {"status": "succeeded"}

    async def _recheckin_unlit(self, access_token: str) -> bool:
        """兜底补签一次；成功或已签到返回 True，失败返回 False(不抛错)。"""
        from .codebuddy import WorkBuddyClient

        client = WorkBuddyClient(
            base_url=self._settings.codebuddy_checkin_base,
            timeout=float(self._settings.checkin_request_timeout_seconds),
        )
        try:
            result = await client.checkin(auth_mode="bearer", access_token=access_token)
        except Exception:
            return False
        finally:
            await client.aclose()
        return result.outcome in {CheckInOutcome.CLAIMED, CheckInOutcome.ALREADY_CHECKED_IN}

    async def confirm_active_day(
        self,
        access_token: str,
        *,
        account_id: str,
        local_date: str,
        timezone: str,
        overview: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """后置确认：当天本地 succeeded 后，上游是否真的记账点亮。

        已确认(confirmed 非空)时不再访问上游；达到尝试上限仍未点亮则标记 not_lit。
        """
        if self._repository is None:
            return {"status": "repository_missing"}
        if not access_token:
            return {"status": "access_token_missing"}
        current = await self._repository.get_workbuddy_active_day(
            provider="codebuddy", account_id=account_id, local_date=local_date, timezone=timezone,
        )
        if current is None or current.get("status") != "succeeded":
            return {"status": "skip_irrelevant"}
        if current.get("confirmed") is not None:
            return {"status": "confirmed", "confirmed": current.get("confirmed")}
        if overview is None:
            try:
                overview = await self._client.fetch(access_token)
            except Exception:
                overview = None
        repo_kwargs = dict(
            provider="codebuddy", account_id=account_id, local_date=local_date, timezone=timezone,
        )
        if overview is not None and _today_lit(overview, local_date):
            await self._repository.touch_workbuddy_active_day_confirmation(
                **repo_kwargs, confirmed="lit",
            )
            return {"status": "lit"}
        attempts = int(current.get("confirm_attempts") or 0) + 1
        if attempts >= self._settings.growth_active_day_confirm_attempts:
            await self._repository.touch_workbuddy_active_day_confirmation(
                **repo_kwargs, confirmed="not_lit",
            )
            return {"status": "not_lit"}
        await self._repository.touch_workbuddy_active_day_confirmation(**repo_kwargs)
        return {"status": "pending"}

    async def rerun_active_day(
        self,
        access_token: str,
        *,
        account_id: str,
        local_date: str,
        timezone: str,
    ) -> dict[str, str]:
        """手动强制重跑当日 ACP：绕过幂等锁、不前置检查，再次发起确认。"""
        if self._repository is None:
            return {"status": "repository_missing"}
        if not access_token:
            return {"status": "access_token_missing"}
        if self._active_day_client is None:
            self._active_day_client = WorkBuddyActiveDayClient(
                base_url=self._settings.codebuddy_endpoint,
                timeout=float(self._settings.checkin_request_timeout_seconds),
            )
        try:
            await self._active_day_client.run(access_token)
        except ActiveDayError as error:
            await self._repository.replace_workbuddy_active_day_result(
                provider="codebuddy", account_id=account_id, local_date=local_date, timezone=timezone,
                status="failed", error_code=error.code,
            )
            return {"status": "failed", "error_code": error.code}
        except Exception as error:
            await self._repository.replace_workbuddy_active_day_result(
                provider="codebuddy", account_id=account_id, local_date=local_date, timezone=timezone,
                status="failed", error_code=type(error).__name__,
            )
            return {"status": "failed", "error_code": type(error).__name__}
        await self._repository.replace_workbuddy_active_day_result(
            provider="codebuddy", account_id=account_id, local_date=local_date, timezone=timezone,
            status="succeeded",
        )
        return {"status": "succeeded"}

    async def run(
        self,
        access_token: str,
        *,
        account_id: str | None = None,
        local_date: str | None = None,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        """执行所有已启用步骤，返回结构化结果。"""
        results: dict[str, Any] = {key: _skipped() for key in _STEP_KEYS}
        if not access_token:
            return {**results, "error": "access_token_missing"}
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
        if self._settings.growth_auto_active_day:
            if account_id and local_date and timezone:
                results["active_day"] = await self._guard(
                    lambda: self.run_active_day(
                        access_token,
                        account_id=account_id, local_date=local_date, timezone=timezone,
                        overview=overview,
                    )
                )
            else:
                results["active_day"] = {
                    "status": "skipped", "detail": "active_day_context_missing",
                }
        return results

    async def run_step(
        self,
        access_token: str,
        step: str,
        *,
        account_id: str | None = None,
        local_date: str | None = None,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        """只执行单个步骤，返回该步骤的结构化结果。"""
        if step not in _STEP_KEYS:
            return {"status": "failed", "detail": f"unknown_step:{step}"}
        if not access_token:
            return {"status": "failed", "detail": "access_token_missing"}
        if step == "active_day":
            if not (account_id and local_date and timezone):
                return {"status": "skipped", "detail": "active_day_context_missing"}
            return await self._guard(
                lambda: self.run_active_day(
                    access_token,
                    account_id=account_id, local_date=local_date, timezone=timezone,
                )
            )
        try:
            overview = await self._client.fetch(access_token)
        except Exception as error:
            return {"status": "failed", "detail": f"fetch_failed:{type(error).__name__}"}
        handlers: dict[str, Callable[[], Awaitable[dict[str, Any]]]] = {
            "tasks": lambda: self._step_tasks(access_token, overview),
            "lottery": lambda: self._step_lottery(access_token, overview),
            "travel": lambda: self._step_travel(access_token),
            "redeem": lambda: self._step_redeem(access_token),
            "buddy_open": lambda: self._step_buddy_open(access_token),
        }
        return await self._guard(handlers[step])

    async def _guard(self, operation: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        try:
            return await operation()
        except Exception as error:
            logger.warning("growth automation step failed: %s", type(error).__name__)
            return {"status": "failed", "detail": f"error:{type(error).__name__}"}

    async def _step_tasks(self, token: str, overview: dict[str, Any]) -> dict[str, Any]:
        tasks = overview.get("tasks", [])
        if not isinstance(tasks, list):
            return {"status": "failed", "detail": "invalid_tasks"}
        tasks = [task for task in tasks if isinstance(task, dict)]
        accepted = await self._accept_pending(token, tasks)
        claimed, total_credits = await self._claim_completed(token, tasks)
        detail = f"接受 {accepted} 个任务，领取 {claimed} 个奖励"
        if total_credits:
            detail += f"，获得 {total_credits} 积分"
        return {
            "status": "completed",
            "accepted": accepted,
            "claimed": claimed,
            "reward_credits": total_credits,
            "detail": detail,
        }

    async def _accept_pending(self, token: str, tasks: list[dict[str, Any]]) -> int:
        pending = [t["task_code"] for t in tasks
                    if _task_status(t) == "not_accepted"
                    and not t.get("locked") and t.get("task_code")]
        if not pending:
            return 0
        try:
            await self._client.accept_tasks(token, pending)
            return len(pending)
        except GrowthUnavailableError:
            return 0

    async def _claim_completed(self, token: str, tasks: list[dict[str, Any]]) -> tuple[int, int]:
        claimed = 0
        total_credits = 0
        for task in tasks:
            if not _task_done(task) or not task.get("has_reward"):
                continue
            if _reward_already_claimed(task):
                continue
            code = task.get("task_code")
            if not code:
                continue
            try:
                result = await self._client.claim_task(token, code)
                claimed += 1
                total_credits += _extract_credits(result, task.get("reward_credit"))
            except GrowthUnavailableError:
                continue
        return claimed, total_credits

    async def _step_lottery(self, token: str, overview: dict[str, Any]) -> dict[str, Any]:
        chances = (overview.get("lottery") or {}).get("available_chances") or 0
        if not chances:
            return {"status": "no_chances", "drawn": 0, "available": 0, "detail": "暂无抽奖次数"}
        drawn = 0
        total_credits = 0
        for _ in range(min(chances, 10)):
            try:
                result = await self._client.lottery_draw(token)
                drawn += 1
                total_credits += _extract_credits(result)
            except GrowthUnavailableError:
                break
        detail = f"抽奖 {drawn}/{chances} 次"
        if total_credits:
            detail += f"，获得 {total_credits} 积分"
        return {
            "status": "completed",
            "drawn": drawn,
            "available": chances,
            "reward_credits": total_credits,
            "detail": detail,
        }

    async def _step_travel(self, token: str) -> dict[str, Any]:
        try:
            status = await self._client.travel_status(token)
        except GrowthUnavailableError as error:
            return {"status": "failed", "detail": f"status_failed:{error}"}
        state = status.get("state")
        if state == "arrived":
            return await self._claim_travel(token)
        if state == "traveling":
            return {"status": "skipped", "detail": "Buddy 正在旅行中"}
        if status.get("daily_limit_reached") is False:
            return await self._depart_travel(token)
        return {"status": "daily_limit_reached", "detail": "今日旅行次数已用完"}

    async def _claim_travel(self, token: str) -> dict[str, Any]:
        try:
            result = await self._client.travel_claim(token)
            total_credits = _extract_credits(result)
            detail = "旅行已结束，已领取奖励"
            if total_credits:
                detail += f"，获得 {total_credits} 积分"
            return {"status": "completed", "reward_credits": total_credits, "detail": detail}
        except GrowthUnavailableError as error:
            return {"status": "failed", "detail": f"claim_failed:{error}"}

    async def _depart_travel(self, token: str) -> dict[str, Any]:
        try:
            config = await self._client.travel_config(token)
            locations = config.get("locations") or []
            if not locations:
                return {"status": "skipped", "detail": "暂无可用旅行地点"}
            first = locations[0]
            location_id = first.get("id") if isinstance(first, dict) else None
            if not isinstance(location_id, int):
                return {"status": "failed", "detail": "invalid_location"}
            await self._client.travel_depart(token, location_id)
            return {"status": "completed", "detail": f"Buddy 已出发前往 {first.get('name', '未知地点')}"}
        except GrowthUnavailableError as error:
            return {"status": "failed", "detail": f"depart_failed:{error}"}

    async def _step_redeem(self, token: str) -> dict[str, Any]:
        tier = self._settings.growth_redeem_tier
        if tier == "off":
            return {"status": "skipped", "detail": "兑换已关闭"}
        required = _REDEEM_DAYS.get(tier)
        if not required:
            return {"status": "failed", "detail": f"unknown_tier:{tier}"}
        try:
            summary = await self._client.redeem_summary(token)
        except GrowthUnavailableError as error:
            return {"status": "failed", "detail": f"summary_failed:{error}"}
        remaining = summary.get("remaining_days") or 0
        if remaining < required:
            return {
                "status": "insufficient",
                "remaining_days": remaining,
                "required_days": required,
                "detail": f"连登 {remaining}/{required} 天，还差 {required - remaining} 天",
            }
        tier_status_key = {"7d": "starter_status", "14d": "advanced_status", "28d": "legendary_status"}[tier]
        if summary.get(tier_status_key) == "unlocked":
            try:
                await self._client.redeem(token, tier)
                return {"status": "completed", "tier": tier, "detail": f"已兑换 {tier} 档奖励"}
            except GrowthUnavailableError as error:
                return {"status": "failed", "detail": f"redeem_failed:{error}"}
        return {"status": "skipped", "detail": f"档位 {tier} 尚未解锁"}

    async def _step_buddy_open(self, token: str) -> dict[str, Any]:
        try:
            quota = await self._client.buddy_quota(token)
        except GrowthUnavailableError as error:
            return {"status": "failed", "detail": f"quota_failed:{error}"}
        affordable = quota.get("affordable") or 0
        if not affordable:
            return {"status": "skipped", "detail": "能量不足，无法抽取 Buddy"}
        try:
            await self._client.buddy_open(token, count=min(affordable, quota.get("max_open_count") or 1))
            return {"status": "completed", "opened": affordable, "detail": f"抽取了 {affordable} 个 Buddy"}
        except GrowthUnavailableError as error:
            return {"status": "failed", "detail": f"open_failed:{error}"}


def _skipped() -> dict[str, Any]:
    return {"status": "skipped", "detail": "未启用"}


def _task_done(task: dict[str, Any]) -> bool:
    if _task_status(task) in {"completed", "claimed"}:
        return True
    current = task.get("progress_current")
    target = task.get("progress_target")
    if isinstance(current, (int, float)) and isinstance(target, (int, float)):
        return current >= target
    return False


def _reward_already_claimed(task: dict[str, Any]) -> bool:
    if _task_status(task) == "claimed":
        return True
    if task.get("reward_claimed") or task.get("claimed") or task.get("is_claimed"):
        return True
    receive_status = str(task.get("receive_status") or "").strip().lower()
    return receive_status in ("claimed", "received")


def _task_status(task: dict[str, Any]) -> str:
    value = task.get("accept_status")
    return str(value).strip().lower() if value is not None else ""


def _extract_credits(result: dict[str, Any], fallback: Any = None) -> int:
    """从 WorkBuddy API 响应中提取获得的积分数量。"""
    for key in ("reward_credit", "credits", "credit", "reward_credits", "amount"):
        value = result.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    reward = result.get("reward")
    if isinstance(reward, dict):
        for key in ("credit", "credits", "amount"):
            value = reward.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
    if isinstance(fallback, (int, float)) and fallback > 0:
        return int(fallback)
    return 0
