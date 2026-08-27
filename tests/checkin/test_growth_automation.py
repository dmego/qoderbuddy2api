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
    def __init__(self) -> None:
        self.claims = 0
        self.finished: list[dict[str, str | None]] = []
        self.touched: list[dict[str, str | None]] = []
        self.replaced: list[dict[str, str | None]] = []
        self.row: dict[str, object] | None = None

    async def claim_workbuddy_active_day(self, **kwargs) -> bool:
        self.claims += 1
        if self.row is None:
            self.row = {**kwargs, "status": "running", "confirm_attempts": 0}
        return self.claims == 1

    async def finish_workbuddy_active_day(self, **kwargs) -> None:
        self.finished.append(kwargs)
        if self.row is not None:
            self.row["status"] = kwargs["status"]
            self.row["error_code"] = kwargs.get("error_code")
            if kwargs.get("confirmed"):
                self.row["confirmed"] = kwargs["confirmed"]

    async def get_workbuddy_active_day(self, **_kwargs) -> dict | None:
        return dict(self.row) if self.row else None

    async def touch_workbuddy_active_day_confirmation(self, **kwargs) -> None:
        self.touched.append(kwargs)
        if self.row is not None:
            self.row["confirm_attempts"] = int(self.row.get("confirm_attempts") or 0) + 1
            if kwargs.get("confirmed"):
                self.row["confirmed"] = kwargs["confirmed"]

    async def replace_workbuddy_active_day_result(self, **kwargs) -> None:
        self.replaced.append(kwargs)
        self.row = {**kwargs, "confirmed": None, "confirm_attempts": 0}


class FakeActiveDayClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def run(self, _token: str) -> None:
        self.calls += 1
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
async def test_active_day_is_daily_idempotent_and_records_safe_failure() -> None:
    repository = FakeRepository()
    active_day = FakeActiveDayClient(ActiveDayError("rpc_timeout"))
    settings = Settings(growth_auto_active_day=True)
    automation = GrowthAutomation(
        settings, FakeGrowthClient({}), repository=repository, active_day_client=active_day
    )

    first = await automation.run_active_day(
        "token", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai"
    )
    second = await automation.run_active_day(
        "token", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai"
    )

    assert first == {"status": "failed", "error_code": "rpc_timeout"}
    assert second == {"status": "already_claimed"}
    assert active_day.calls == 1
    assert repository.finished[0]["error_code"] == "rpc_timeout"


@pytest.mark.asyncio
async def test_run_runs_active_day_when_enabled_with_context() -> None:
    repository = FakeRepository()
    active_day = FakeActiveDayClient()
    settings = Settings(growth_auto_active_day=True)
    automation = GrowthAutomation(
        settings, FakeGrowthClient({}), repository=repository, active_day_client=active_day
    )

    result = await automation.run(
        "token", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai"
    )

    assert result["active_day"]["status"] == "succeeded"
    assert active_day.calls == 1

    # 同一账号同一天再次执行幂等。
    second = await automation.run(
        "token", account_id="cb-1", local_date="2026-08-05", timezone="Asia/Shanghai"
    )
    assert second["active_day"]["status"] == "already_claimed"
    assert active_day.calls == 1


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

    assert result == {"status": "skipped_external"}
    assert active_day.calls == 0
    assert repository.finished[0]["status"] == "skipped_external"
    assert repository.finished[0]["confirmed"] == "lit"


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
async def test_run_active_day_rechecks_checkin_when_not_lit() -> None:
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
    assert result["status"] == "succeeded"
    assert active_day.calls == 1


@pytest.mark.asyncio
async def test_run_active_day_recheck_skips_when_already_lit() -> None:
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
    assert result["status"] == "succeeded"
    assert active_day.calls == 1


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
