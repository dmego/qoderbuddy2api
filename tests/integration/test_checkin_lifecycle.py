"""Check-in batch cancellation and lifecycle contracts."""

from __future__ import annotations

import asyncio

import pytest
from checkin_service_support import BlockingClient, SequenceClient, registry, seed, service

from qb2api.checkin.service import CheckinInProgressError


@pytest.mark.asyncio
async def test_cancelled_batch_does_not_leave_running_row(checkin_context) -> None:
    repository, vault = checkin_context
    await seed(repository, vault, "codebuddy", "cb-main")
    account_registry = await registry(repository, vault)
    workbuddy = BlockingClient()
    checkin_service = service(
        repository, vault, account_registry, workbuddy=workbuddy, qoder=SequenceClient("qoder", []),
        codebuddy_enabled=True, qoder_enabled=False,
    )
    task = asyncio.create_task(checkin_service.run_batch(trigger="scheduler"))
    await workbuddy.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    row = await (await repository.db.execute("SELECT status FROM checkin_runs")).fetchone()
    assert row is not None
    assert row[0] == "cancelled"
    await checkin_service.close()


@pytest.mark.asyncio
async def test_started_batch_returns_durable_operation_and_closes_cleanly(checkin_context) -> None:
    repository, vault = checkin_context
    await seed(repository, vault, "codebuddy", "cb-main")
    account_registry = await registry(repository, vault)
    workbuddy = BlockingClient()
    checkin_service = service(
        repository, vault, account_registry, workbuddy=workbuddy, qoder=SequenceClient("qoder", []),
        codebuddy_enabled=True, qoder_enabled=False,
    )

    run_id = await checkin_service.start_batch(trigger="manual", skip_already_done=False)
    await workbuddy.started.wait()
    assert checkin_service.active_run_id == run_id
    with pytest.raises(CheckinInProgressError, match="checkin_run_in_progress"):
        await checkin_service.start_batch(trigger="manual")

    await checkin_service.close()
    run = await repository.get_checkin_run(run_id)
    assert run is not None
    assert run["status"] == "cancelled"
