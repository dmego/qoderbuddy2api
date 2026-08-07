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


@pytest.mark.asyncio
async def test_active_day_confirmation_persists_over_attempts(checkin_context) -> None:
    repository, _vault = checkin_context
    kwargs = dict(provider="codebuddy", account_id="cb-main",
                  local_date="2026-08-05", timezone="Asia/Shanghai")
    await repository.claim_workbuddy_active_day(**kwargs)
    await repository.finish_workbuddy_active_day(**kwargs, status="succeeded")

    await repository.touch_workbuddy_active_day_confirmation(**kwargs)
    row = await repository.get_workbuddy_active_day(**kwargs)
    assert row["confirm_attempts"] == 1
    assert row["confirmed"] is None

    await repository.touch_workbuddy_active_day_confirmation(**kwargs, confirmed="lit")
    row = await repository.get_workbuddy_active_day(**kwargs)
    assert row["confirmed"] == "lit"
    assert row["confirmed_at"] is not None
    assert row["confirm_attempts"] == 2


@pytest.mark.asyncio
async def test_active_day_replace_result_resets_confirm(checkin_context) -> None:
    repository, _vault = checkin_context
    kwargs = dict(provider="codebuddy", account_id="cb-main",
                  local_date="2026-08-05", timezone="Asia/Shanghai")
    await repository.claim_workbuddy_active_day(**kwargs)
    await repository.finish_workbuddy_active_day(**kwargs, status="succeeded")
    await repository.touch_workbuddy_active_day_confirmation(**kwargs, confirmed="not_lit")

    await repository.replace_workbuddy_active_day_result(**kwargs, status="succeeded")

    row = await repository.get_workbuddy_active_day(**kwargs)
    assert row["status"] == "succeeded"
    assert row["confirmed"] is None
    assert row["confirm_attempts"] == 0


@pytest.mark.asyncio
async def test_active_day_finish_skipped_external_records_confirmed(checkin_context) -> None:
    repository, _vault = checkin_context
    kwargs = dict(provider="codebuddy", account_id="cb-main",
                  local_date="2026-08-05", timezone="Asia/Shanghai")
    await repository.claim_workbuddy_active_day(**kwargs)

    await repository.finish_workbuddy_active_day(**kwargs, status="skipped_external", confirmed="lit")

    row = await repository.get_workbuddy_active_day(**kwargs)
    assert row["status"] == "skipped_external"
    assert row["confirmed"] == "lit"


@pytest.mark.asyncio
async def test_active_day_finish_rejects_invalid_status(checkin_context) -> None:
    repository, _vault = checkin_context
    kwargs = dict(provider="codebuddy", account_id="cb-main",
                  local_date="2026-08-05", timezone="Asia/Shanghai")
    with pytest.raises(ValueError):
        await repository.finish_workbuddy_active_day(**kwargs, status="bogus")
