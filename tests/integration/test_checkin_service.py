"""Check-in scheduler-selection and retry contracts."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from checkin_service_support import SequenceClient, registry, seed, service, success

from qb2api.checkin.models import CheckInOutcome, CheckInResult


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
async def test_scheduled_checkin_does_not_claim_active_day(checkin_context) -> None:
    """活跃日与签到解耦后，签到批次不应再 claim 成长中心活跃日。"""
    repository, vault = checkin_context
    await seed(repository, vault, "codebuddy", "cb-growth")
    account_registry = await registry(repository, vault)
    checkin_service = service(
        repository, vault, account_registry,
        workbuddy=SequenceClient("codebuddy", [success("codebuddy", "cb-growth")]),
        qoder=SequenceClient("qoder", []), codebuddy_enabled=True, qoder_enabled=False,
    )

    await checkin_service.run_batch(trigger="scheduler")
    await checkin_service.close()

    timezone = "Asia/Shanghai"
    today = datetime.now(ZoneInfo(timezone)).date().isoformat()
    claimed = await repository.get_workbuddy_active_day(
        "codebuddy", "cb-growth", today, timezone=timezone,
    )
    assert claimed is None
