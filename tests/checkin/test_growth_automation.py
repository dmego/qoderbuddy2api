"""Growth automation safety and step-isolation contracts."""

from __future__ import annotations

import pytest

from qb2api.checkin.active_day import ActiveDayError
from qb2api.checkin.growth_automation import GrowthAutomation
from qb2api.config import Settings


class FakeGrowthClient:
    def __init__(self, overview):
        self.overview = overview
        self.calls: list[str] = []

    async def fetch(self, _token):
        return self.overview

    async def aclose(self):
        return None

    async def travel_status(self, _token):
        self.calls.append("travel_status")
        raise RuntimeError("test failure")


class FakeRepository:
    def __init__(self) -> None:
        self.claims = 0
        self.finished: list[dict[str, str | None]] = []

    async def claim_workbuddy_active_day(self, **_kwargs) -> bool:
        self.claims += 1
        return self.claims == 1

    async def finish_workbuddy_active_day(self, **kwargs) -> None:
        self.finished.append(kwargs)


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
        growth_auto_buddy_open=False,
    )
    result = await GrowthAutomation(settings, client).run("token")
    for key in ("tasks", "lottery", "travel", "redeem", "buddy_open"):
        assert result[key] == {"status": "skipped", "detail": "未启用"}


@pytest.mark.asyncio
async def test_step_failure_isolated_and_sanitized() -> None:
    client = FakeGrowthClient({"tasks": [], "lottery": {}})
    settings = Settings(
        growth_auto_tasks=False, growth_auto_lottery=False,
        growth_auto_travel=True, growth_auto_redeem=False,
        growth_auto_buddy_open=False,
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
