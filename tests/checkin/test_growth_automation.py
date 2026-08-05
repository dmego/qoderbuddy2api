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
    assert result == {
        "tasks": "skipped", "lottery": "skipped",
        "travel": "skipped", "redeem": "skipped",
        "buddy_open": "skipped",
    }


@pytest.mark.asyncio
async def test_step_failure_isolated_and_sanitized() -> None:
    client = FakeGrowthClient({"tasks": [], "lottery": {}})
    settings = Settings(
        growth_auto_tasks=False, growth_auto_lottery=False,
        growth_auto_travel=True, growth_auto_redeem=False,
        growth_auto_buddy_open=False,
    )
    result = await GrowthAutomation(settings, client).run("token")
    assert result["travel"] == "failed:RuntimeError"
    assert "test failure" not in str(result)
