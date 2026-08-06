"""Growth automation safety and step-isolation contracts."""

from __future__ import annotations

import pytest

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
