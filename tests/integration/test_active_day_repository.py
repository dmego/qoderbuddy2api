"""Persistence contracts for WorkBuddy active-day reservations."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_workbuddy_active_day_claim_is_idempotent(checkin_context) -> None:
    repository, _vault = checkin_context

    first = await repository.claim_workbuddy_active_day(
        provider="codebuddy", account_id="cb-main",
        local_date="2026-08-05", timezone="Asia/Shanghai",
    )
    second = await repository.claim_workbuddy_active_day(
        provider="codebuddy", account_id="cb-main",
        local_date="2026-08-05", timezone="Asia/Shanghai",
    )

    assert first is True
    assert second is False
