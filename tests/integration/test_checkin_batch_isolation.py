"""Check-in per-account failure isolation contracts."""

from __future__ import annotations

import pytest
from checkin_service_support import SequenceClient, registry, seed, service, success

from qb2api.checkin.models import CheckInOutcome


@pytest.mark.asyncio
async def test_batch_isolates_account_failure_and_preserves_chat_purpose(checkin_context) -> None:
    repository, vault = checkin_context
    await seed(repository, vault, "codebuddy", "cb-a")
    await seed(repository, vault, "codebuddy", "cb-b")
    account_registry = await registry(repository, vault)
    workbuddy = SequenceClient("codebuddy", [RuntimeError("account-a-failed"), success("codebuddy", "cb-b")])
    checkin_service = service(
        repository, vault, account_registry, workbuddy=workbuddy, qoder=SequenceClient("qoder", []),
        codebuddy_enabled=True, qoder_enabled=False,
    )

    batch = await checkin_service.run_batch(trigger="scheduler")

    assert [item["account_id"] for item in batch.results] == ["cb-a", "cb-b"]
    assert [item["outcome"] for item in batch.results] == [
        CheckInOutcome.FAILED.value, CheckInOutcome.CLAIMED.value,
    ]
    for account_id in ("cb-a", "cb-b"):
        purposes = await repository.list_purposes("codebuddy", account_id)
        chat = next(item for item in purposes if item["purpose"] == "chat")
        assert chat["status"] == "active"
        assert chat["verification_status"] == "not_required"
    await checkin_service.close()
