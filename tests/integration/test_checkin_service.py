"""Check-in scheduler-selection and retry contracts."""

from __future__ import annotations

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
