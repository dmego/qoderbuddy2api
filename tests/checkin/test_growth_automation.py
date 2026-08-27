"""Growth automation safety and step-isolation contracts."""

from __future__ import annotations

import pytest

from qb2api.checkin.active_day import ActiveDayError
from qb2api.checkin.growth_automation import GrowthAutomation, _today_lit
from qb2api.config import Settings


class FakeGrowthClient:
    def __init__(self, overview):
        self.overview = overview
        self.calls: list[str] = []

    async def fetch(self, _token):
        return self.overview

    async def accept_tasks(self, _token, task_codes):
        self.calls.extend(f"accept:{code}" for code in task_codes)

    async def claim_task(self, _token, task_code):
        self.calls.append(f"claim:{task_code}")
        return {}

    async def aclose(self):
        return None

    async def travel_status(self, _token):
        self.calls.append("travel_status")
        raise RuntimeError("test failure")


class FakeRepository:
    """按 local_date 分行的内存仓储，支持跨日预算重置场景。"""

    def __init__(self) -> None:
        self.claims = 0
        self.finished: list[dict[str, str | None]] = []
        self.touched: list[dict[str, str | None]] = []
        self.replaced: list[dict[str, str | None]] = []
        self.rows: dict[str, dict[str, object]] = {}
        self.last_key: str | None = None

    def _row_for(self, kwargs: dict) -> dict[str, object] | None:
        return self.rows.get(kwargs["local_date"])

    @property
    def row(self) -> dict[str, object] | None:
        return self.rows.get(self.last_key)

    async def claim_workbuddy_active_day(self, **kwargs) -> bool:
        self.claims += 1
        self.last_key = kwargs["local_date"]
        if kwargs["local_date"] in self.rows:
            return False
        self.rows[kwargs["local_date"]] = {
            **kwargs, "status": "running", "confirm_attempts": 0,
        }
        return True

    async def finish_workbuddy_active_day(self, **kwargs) -> None:
        self.finished.append(kwargs)
        row = self._row_for(kwargs)
        if row is not None:
            row["status"] = kwargs["status"]
            row["error_code"] = kwargs.get("error_code")
            if kwargs.get("confirmed"):
                row["confirmed"] = kwargs["confirmed"]

    async def get_workbuddy_active_day(self, **kwargs) -> dict | None:
        self.last_key = kwargs["local_date"]
        row = self.rows.get(kwargs["local_date"])
        return dict(row) if row else None

    async def touch_workbuddy_active_day_confirmation(self, **kwargs) -> None:
        self.touched.append(kwargs)
        row = self._row_for(kwargs)
        if row is not None:
            row["confirm_attempts"] = int(row.get("confirm_attempts") or 0) + 1
            if kwargs.get("confirmed"):
                row["confirmed"] = kwargs["confirmed"]

    async def replace_workbuddy_active_day_result(self, **kwargs) -> None:
        self.replaced.append(kwargs)
        key = kwargs["local_date"]
        self.last_key = key
        self.rows[key] = {**kwargs, "confirmed": None, "confirm_attempts": 0}


class FakeActiveDayClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0
        self.prompts: list[str] = []

    async def run(self, _token: str, *, prompt: str = "") -> None:
        self.calls += 1
        self.prompts.append(prompt)
        if self.error:
            raise self.error

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_disabled_automation_does_not_fetch() -> None:
    client = FakeGrowthClient({})
    settings = Settings(
        growth_auto_tasks=False, growth_auto_lottery=False,
        growth_auto_travel=False, growth_auto_redeem=False,
        growth_auto_buddy_open=False, growth_auto_active_day=False,
    )
    result = await GrowthAutomation(settings, client).run("token")
    for key in ("tasks", "lottery", "travel", "redeem", "buddy_open", "active_day"):
        assert result[key] == {"status": "skipped", "detail": "未启用"}


@pytest.mark.asyncio
async def test_step_failure_isolated_and_sanitized() -> None:
    client = FakeGrowthClient({"tasks": [], "lottery": {}})
    settings = Settings(
        growth_auto_tasks=False, growth_auto_lottery=False,
        growth_auto_travel=True, growth_auto_redeem=False,
        growth_auto_buddy_open=False, growth_auto_active_day=False,
    )
    result = await GrowthAutomation(settings, client).run("token")
    assert result["travel"]["status"] == "failed"
    assert "test failure" not in str(result)


@pytest.mark.asyncio
async def test_run_step_executes_single_step() -> None:
    client = FakeGrowthClient({"tasks": [], "lottery": {"available_chances": 0}})
    settings = Settings()
    automation = GrowthAutomation(settings, client)
    result = await automation.run_step("token", "lottery")
    assert result["status"] == "no_chances"
    assert result["detail"] == "暂无抽奖次数"


@pytest.mark.asyncio
async def test_run_step_rejects_unknown_step() -> None:
    client = FakeGrowthClient({})
    settings = Settings()
    automation = GrowthAutomation(settings, client)
    result = await automation.run_step("token", "bogus")
    assert result["status"] == "failed"
    assert "unknown_step" in result["detail"]


@pytest.mark.asyncio
async def test_claimed_tasks_are_not_claimed_again() -> None:
    client = FakeGrowthClient({
        "tasks": [
            {"task_code": "already-claimed", "accept_status": "claimed",
             "progress_current": 1, "progress_target": 1, "has_reward": True},
            {"task_code": "ready-to-claim", "accept_status": "completed",
             "progress_current": 1, "progress_target": 1, "has_reward": True},
        ],
    })
    settings = Settings(
        growth_auto_tasks=True, growth_auto_lottery=False,
        growth_auto_travel=False, growth_auto_redeem=False,
        growth_auto_buddy_open=False,
    )

    result = await GrowthAutomation(settings, client).run("token")

    assert result["tasks"]["claimed"] == 1
    assert client.calls == ["claim:ready-to-claim"]


@pytest.mark.asyncio
async def test_active_day_failure_is_retried_within_daily_budget() -> None:
    """当日失败不再死锁：预算内可反复重试，官方点亮后收敛为 skipped_external。"""
    repository = FakeRepository()
    active_day = FakeActiveDayClient(ActiveDayError("rpc_timeout"))
    settings = Settings(growth_auto_active_day=True)
    automation = GrowthAutomation(
        settings, FakeGrowthClient({}), repository=repository, active_day_client=active_day
    )
    # 补签兜底关闭，专注 ACP 失败重试路径
    object.__setattr__(settings, "growth_auto_active_day_recheckin", False)

    first = await automation.run_active_day(
        "token", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai"
    )
    second = await automation.run_active_day(
        "token", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai"
    )

    assert first["status"] == "failed"
    assert second["status"] == "failed"
    # 每 tick 组合拳最多 3 轮；两个 tick 共 6 次，全部计入失败
    assert active_day.calls == 6
    assert all(f["status"] == "failed" for f in repository.finished)


@pytest.mark.asyncio
async def test_run_runs_active_day_when_enabled_with_context() -> None:
    repository = FakeRepository()
    active_day = FakeActiveDayClient()
    settings = Settings(growth_auto_active_day=True)
    lit_overview = {"heatmap": {"cells": [{"date": "2026-08-05", "score": 2}]}}
    automation = GrowthAutomation(
        settings, FakeGrowthClient(lit_overview), repository=repository, active_day_client=active_day
    )

    result = await automation.run(
        "token", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai",
    )

    # 官方已点亮 -> 直接跳过，不消耗 ACP
    assert result["active_day"]["status"] == "skipped_external"
    assert active_day.calls == 0


@pytest.mark.asyncio
async def test_run_skips_active_day_when_toggle_off_or_context_missing() -> None:
    settings = Settings(growth_auto_active_day=False)
    automation = GrowthAutomation(settings, FakeGrowthClient({}), repository=FakeRepository())
    result = await automation.run(
        "token", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai"
    )
    assert result["active_day"] == {"status": "skipped", "detail": "未启用"}

    enabled = Settings(growth_auto_active_day=True)
    automation_enabled = GrowthAutomation(enabled, FakeGrowthClient({}), repository=FakeRepository())
    missing = await automation_enabled.run("token")
    assert missing["active_day"] == {"status": "skipped", "detail": "active_day_context_missing"}


@pytest.mark.asyncio
async def test_run_step_active_day_requires_context() -> None:
    settings = Settings(growth_auto_active_day=True)
    automation = GrowthAutomation(settings, FakeGrowthClient({}), repository=FakeRepository())

    missing = await automation.run_step("token", "active_day")
    assert missing["status"] == "skipped"
    assert missing["detail"] == "active_day_context_missing"

    bogus = await automation.run_step("token", "bogus")
    assert bogus["status"] == "failed"
    assert "unknown_step" in bogus["detail"]


@pytest.mark.asyncio
async def test_run_active_day_skips_when_already_lit_externally() -> None:
    repository = FakeRepository()
    active_day = FakeActiveDayClient()
    automation = GrowthAutomation(
        Settings(growth_auto_active_day=True), FakeGrowthClient({}),
        repository=repository, active_day_client=active_day,
    )
    overview = {
        "heatmap": {
            "cells": [{"date": "2026-08-05", "score": 2}],
            "today": {"date": "2026-08-04", "is_active": True, "status_text": "今日已活跃"},
        },
    }

    result = await automation.run_active_day(
        "token", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai",
        overview=overview,
    )

    # 官方已点亮 => 立即收敛；本轮内不额外消耗 ACP
    assert result["status"] == "skipped_external"
    assert active_day.calls == 0
    assert repository.finished[0]["status"] == "succeeded"
    assert repository.finished[0]["confirmed"] == "lit"


@pytest.mark.asyncio
async def test_run_active_day_pending_when_acp_ok_but_official_lags() -> None:
    """ACP 三轮成功而官方未记账 => 待确认（非失败）；prompt 多样化。"""
    repository = FakeRepository()
    active_day = FakeActiveDayClient()
    automation = GrowthAutomation(
        Settings(growth_auto_active_day=True), FakeGrowthClient({}),
        repository=repository, active_day_client=active_day,
    )
    object.__setattr__(
        Settings(growth_auto_active_day=True), "growth_auto_active_day_recheckin", False
    )
    automation._settings.growth_auto_active_day_recheckin = False
    overview = {"heatmap": {"cells": [{"date": "2026-08-05", "score": 0}]}}

    result = await automation.run_active_day(
        "token", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai",
        overview=overview,
    )

    assert result["status"] == "pending_confirmation"
    assert active_day.calls == 3
    assert len(set(active_day.prompts)) == 3  # prompt 多样化
    assert repository.row["status"] == "pending_confirmation"


@pytest.mark.asyncio
async def test_confirm_active_day_marks_lit_when_today_counted() -> None:
    repository = FakeRepository()
    automation = GrowthAutomation(Settings(growth_auto_active_day=True), FakeGrowthClient({}), repository=repository)
    await repository.claim_workbuddy_active_day(
        provider="codebuddy", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai",
    )
    await repository.finish_workbuddy_active_day(
        provider="codebuddy", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai",
        status="succeeded",
    )

    overview = {"heatmap": {"cells": [{"date": "2026-08-05", "score": 2}]}}
    result = await automation.confirm_active_day(
        "token", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai", overview=overview,
    )

    assert result["status"] == "lit"
    assert repository.touched[-1]["confirmed"] == "lit"


@pytest.mark.asyncio
async def test_today_lit_ignores_utc_misaligned_yesterday() -> None:
    # 官方 today 按上游日界线；本地 08-06 凌晨时 today 仍是 08-05 且 is_active=True。
    overview = {
        "heatmap": {
            "cells": [{"date": "2026-08-06", "score": 0}],
            "today": {"date": "2026-08-05", "is_active": True, "score": 2},
        },
    }
    assert not _today_lit(overview, "2026-08-06")
    assert _today_lit(overview, "2026-08-05")


@pytest.mark.asyncio
async def test_today_lit_cell_score_decides() -> None:
    assert _today_lit(
        {"heatmap": {"cells": [{"date": "2026-08-05", "score": 2}]}}, "2026-08-05",
    )
    assert not _today_lit(
        {"heatmap": {"cells": [{"date": "2026-08-05", "score": 0}]}}, "2026-08-05",
    )
    assert not _today_lit({"heatmap": {}}, "2026-08-05")


@pytest.mark.asyncio
async def test_run_active_day_falls_back_to_acp_after_recheckin() -> None:
    repository = FakeRepository()
    active_day = FakeActiveDayClient()
    settings = Settings(growth_auto_active_day=True, growth_auto_active_day_recheckin=True)
    automation = GrowthAutomation(settings, FakeGrowthClient({}), repository=repository, active_day_client=active_day)
    recheck_calls: list[bool] = []

    async def fake_recheck(_token: str) -> bool:
        recheck_calls.append(True)
        return True

    automation._recheckin_unlit = fake_recheck  # 实例属性覆写方法
    result = await automation.run_active_day(
        "token", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai",
    )

    assert recheck_calls == [True]
    # 上游账本延迟 -> 本 tick 继续以 ACP 组合拳兜底
    assert result["status"] == "pending_confirmation"
    assert active_day.calls == 3


@pytest.mark.asyncio
async def test_run_active_day_extra_rounds_when_unlit() -> None:
    repository = FakeRepository()
    active_day = FakeActiveDayClient()
    settings = Settings(growth_auto_active_day=True, growth_auto_active_day_recheckin=True)
    automation = GrowthAutomation(settings, FakeGrowthClient({}), repository=repository, active_day_client=active_day)
    recheck_calls: list[bool] = []

    async def fake_recheck(_token: str) -> bool:
        recheck_calls.append(True)
        return False

    automation._recheckin_unlit = fake_recheck
    result = await automation.run_active_day(
        "token", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai",
        overview={"heatmap": {"cells": [{"date": "2026-08-05", "score": 0}]}},
    )

    assert recheck_calls == [True]
    assert result["status"] == "pending_confirmation"
    assert active_day.calls == 3


@pytest.mark.asyncio
async def test_confirm_active_day_does_not_mis_lit_on_utc_yesterday() -> None:
    repository = FakeRepository()
    settings = Settings(growth_auto_active_day=True, growth_active_day_confirm_attempts=2)
    automation = GrowthAutomation(settings, FakeGrowthClient({}), repository=repository)
    await repository.claim_workbuddy_active_day(
        provider="codebuddy", account_id="cb-1", local_date="2026-08-06", timezone="Asia/Shanghai",
    )
    await repository.finish_workbuddy_active_day(
        provider="codebuddy", account_id="cb-1", local_date="2026-08-06", timezone="Asia/Shanghai",
        status="succeeded",
    )
    overview = {
        "heatmap": {
            "cells": [{"date": "2026-08-06", "score": 0}],
            "today": {"date": "2026-08-05", "is_active": True},
        },
    }

    first = await automation.confirm_active_day(
        "token", account_id="cb-1", local_date="2026-08-06", timezone="Asia/Shanghai", overview=overview,
    )

    assert first["status"] == "pending"
    assert repository.row.get("confirmed") is None


@pytest.mark.asyncio
async def test_confirm_active_day_terminates_not_lit_after_attempts() -> None:
    repository = FakeRepository()
    settings = Settings(growth_auto_active_day=True, growth_active_day_confirm_attempts=2)
    automation = GrowthAutomation(settings, FakeGrowthClient({}), repository=repository)
    await repository.claim_workbuddy_active_day(
        provider="codebuddy", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai",
    )
    await repository.finish_workbuddy_active_day(
        provider="codebuddy", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai",
        status="succeeded",
    )
    overview = {"heatmap": {"today": {"is_active": False, "score": 0}}}

    first = await automation.confirm_active_day(
        "token", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai", overview=overview,
    )
    second = await automation.confirm_active_day(
        "token", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai", overview=overview,
    )

    assert first["status"] == "pending"
    assert second["status"] == "not_lit"
    assert repository.row["confirmed"] == "not_lit"


@pytest.mark.asyncio
async def test_rerun_active_day_forces_conversation_and_resets_confirm() -> None:
    repository = FakeRepository()
    active_day = FakeActiveDayClient()
    automation = GrowthAutomation(
        Settings(growth_auto_active_day=True), FakeGrowthClient({}),
        repository=repository, active_day_client=active_day,
    )
    await repository.claim_workbuddy_active_day(
        provider="codebuddy", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai",
    )
    await repository.finish_workbuddy_active_day(
        provider="codebuddy", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai",
        status="succeeded",
    )

    result = await automation.rerun_active_day(
        "token", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai",
    )

    assert result == {"status": "succeeded"}
    assert active_day.calls == 1
    assert repository.replaced[0]["status"] == "succeeded"
    assert repository.row["confirmed"] is None
    assert repository.row["confirm_attempts"] == 0


@pytest.mark.asyncio
async def test_active_day_retry_converges_when_recheckin_lights_official() -> None:
    repository = FakeRepository()
    active_day = FakeActiveDayClient(ActiveDayError("first-fail"))
    overview_holder = {"ov": {"heatmap": {"cells": [{"date": "2026-08-05", "score": 0}]}}}

    class FlipClient(FakeGrowthClient):
        async def fetch(self, _token):
            return overview_holder["ov"]

    settings = Settings(
        growth_auto_active_day=True,
        growth_auto_active_day_recheckin=True,
    )
    automation = GrowthAutomation(settings, FlipClient({}), repository=repository, active_day_client=active_day)

    async def recheck_then_light(_token):
        # 第二次起：补签成功并让官方点亮
        if active_day.calls >= 1:
            overview_holder["ov"] = {"heatmap": {"cells": [{"date": "2026-08-05", "score": 2}]}}
            return True
        return False

    automation._recheckin_unlit = recheck_then_light

    first = await automation.run_active_day(
        "token", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai"
    )
    assert first["status"] == "failed"

    second = await automation.run_active_day(
        "token", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai"
    )
    assert second["status"] == "skipped_external"
    assert repository.row["confirmed"] == "lit"


@pytest.mark.asyncio
async def test_active_day_retro_fixes_previous_day_label() -> None:
    """昨日本地误标 lit 而官方空 -> 自动改 not_lit；官方亮 -> 保持 lit。"""
    from datetime import date, timedelta

    today = "2026-08-27"
    yesterday = (date(2026, 8, 27) - timedelta(days=1)).isoformat()
    repository = FakeRepository()
    active_day = FakeActiveDayClient()
    automation = GrowthAutomation(
        Settings(growth_auto_active_day=True), FakeGrowthClient({}),
        repository=repository, active_day_client=active_day,
    )
    object.__setattr__(settings := Settings(growth_auto_active_day=True), "growth_auto_active_day_recheckin", False)
    automation._settings = settings

    # 构造昨日行: succeeded + lit，但官方昨格 score=0（模拟历史上 UTC 日界线误判）
    await repository.claim_workbuddy_active_day(
        provider="codebuddy", account_id="cb-1",
        local_date=yesterday, timezone="Asia/Shanghai",
    )
    await repository.finish_workbuddy_active_day(
        provider="codebuddy", account_id="cb-1",
        local_date=yesterday, timezone="Asia/Shanghai",
        status="succeeded", confirmed="lit",
    )

    overview = {
        "heatmap": {"cells": [
            {"date": yesterday, "score": 0},
            {"date": today, "score": 0},
        ]},
    }
    await automation.run_active_day(
        "token", account_id="cb-1", local_date=today, timezone="Asia/Shanghai",
        overview=overview,
    )

    yrow = await repository.get_workbuddy_active_day(
        provider="codebuddy", account_id="cb-1",
        local_date=yesterday, timezone="Asia/Shanghai",
    )
    assert yrow["confirmed"] == "not_lit"
