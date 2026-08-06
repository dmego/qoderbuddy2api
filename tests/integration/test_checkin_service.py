"""Check-in scheduler-selection and retry contracts."""

from __future__ import annotations

import pytest
from checkin_service_support import SequenceClient, registry, seed, service, success

from qb2api.checkin.batch import CheckinTarget
from qb2api.checkin.models import CheckInOutcome, CheckInResult


class FakeGrowthAutomation:
    def __init__(self) -> None:
        self.runs: list[str] = []
        self.active_days: list[tuple[str, str, str, str]] = []

    async def run(self, token: str) -> dict[str, str]:
        self.runs.append(token)
        return {"tasks": "skipped"}

    async def run_active_day(self, token: str, *, account_id: str, local_date: str, timezone: str) -> dict[str, str]:
        self.active_days.append((token, account_id, local_date, timezone))
        return {"status": "succeeded"}

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_scheduler_filters_disabled_provider(checkin_context) -> None:
    repository, vault = checkin_context
    await seed(repository, vault, "codebuddy", "cb-main")
    await seed(repository, vault, "qoder", "qd-main")
    account_registry = await registry(repository, vault)
    workbuddy = SequenceClient("codebuddy", [success("codebuddy", "cb-main")])
    qoder = SequenceClient("qoder", [success("qoder", "qd-main")])
    checkin_service = service(
        repository, vault, account_registry, workbuddy=workbuddy, qoder=qoder,
        codebuddy_enabled=False, qoder_enabled=True,
    )

    batch = await checkin_service.run_batch(trigger="scheduler")

    assert [item["account_id"] for item in batch.results] == ["qd-main"]
    assert workbuddy.calls == 0
    assert qoder.calls == 1
    await checkin_service.close()


@pytest.mark.asyncio
async def test_transient_result_retries_and_persists_attempt_count(checkin_context) -> None:
    repository, vault = checkin_context
    await seed(repository, vault, "codebuddy", "cb-main")
    account_registry = await registry(repository, vault)
    workbuddy = SequenceClient("codebuddy", [
        CheckInResult(
            outcome=CheckInOutcome.TRANSIENT_ERROR, provider="codebuddy", account_id="cb-main", http_status=503,
        ),
        success("codebuddy", "cb-main"),
    ])
    checkin_service = service(
        repository, vault, account_registry, workbuddy=workbuddy, qoder=SequenceClient("qoder", []),
        codebuddy_enabled=True, qoder_enabled=False, retry_limit=1,
    )

    batch = await checkin_service.run_batch(trigger="scheduler")
    attempts = await repository.list_checkin_attempts(batch.run_id)

    assert workbuddy.calls == 2
    assert attempts[0]["attempts"] == 2
    assert attempts[0]["outcome"] == CheckInOutcome.CLAIMED.value
    await checkin_service.close()


@pytest.mark.asyncio
async def test_has_pending_targets_returns_false_when_all_accounts_are_terminal(checkin_context) -> None:
    repository, vault = checkin_context
    await seed(repository, vault, "codebuddy", "cb-main")
    account_registry = await registry(repository, vault)
    checkin_service = service(
        repository, vault, account_registry,
        workbuddy=SequenceClient("codebuddy", []), qoder=SequenceClient("qoder", []),
        codebuddy_enabled=True, qoder_enabled=False,
    )

    local_date = checkin_service.local_date_str()
    await repository.set_checkin_daily_state(
        provider="codebuddy", account_id="cb-main", local_date=local_date,
        timezone=checkin_service._settings.checkin_timezone,  # noqa: SLF001
        terminal_outcome=CheckInOutcome.CLAIMED.value,
    )

    assert await checkin_service.has_pending_targets() is False
    await checkin_service.close()


@pytest.mark.asyncio
async def test_status_snapshot_exposes_qoder_checkin_error(checkin_context) -> None:
    repository, vault = checkin_context
    await seed(repository, vault, "qoder", "qd-disabled")
    account_registry = await registry(repository, vault)
    qoder = SequenceClient(
        "qoder",
        [
            CheckInResult(
                outcome=CheckInOutcome.FAILED,
                provider="qoder",
                account_id="qd-disabled",
                http_status=200,
                raw_status="DISABLED",
            )
        ],
    )
    checkin_service = service(
        repository, vault, account_registry, workbuddy=SequenceClient("codebuddy", []),
        qoder=qoder, codebuddy_enabled=False, qoder_enabled=True,
    )

    await checkin_service.run_batch(trigger="manual", skip_already_done=False)
    snapshot = await checkin_service.status_snapshot()

    account = snapshot["eligible_accounts"][0]
    assert account["last_error"] == "qoder_checkin_disabled"
    await checkin_service.close()


@pytest.mark.asyncio
async def test_growth_automation_only_runs_after_scheduled_success(checkin_context) -> None:
    repository, vault = checkin_context
    await seed(repository, vault, "codebuddy", "cb-growth")
    account_registry = await registry(repository, vault)
    growth = FakeGrowthAutomation()
    checkin_service = service(
        repository, vault, account_registry,
        workbuddy=SequenceClient("codebuddy", [success("codebuddy", "cb-growth")]),
        qoder=SequenceClient("qoder", []), codebuddy_enabled=True, qoder_enabled=False,
        growth_automation=growth,
    )

    await checkin_service.run_batch(trigger="scheduler")
    assert growth.runs == []
    assert growth.active_days == [("access-cb-growth", "cb-growth", checkin_service.local_date_str(), "Asia/Shanghai")]
    await checkin_service.run_batch(
        trigger="manual",
        targets=[CheckinTarget(provider="codebuddy", account_id="cb-growth")],
        skip_already_done=False,
    )
    assert growth.runs == []
    assert len(growth.active_days) == 1
    await checkin_service.close()
