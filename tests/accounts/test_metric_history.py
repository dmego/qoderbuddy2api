"""Credit/points metric history persistence."""

from __future__ import annotations

import pytest

from qb2api.accounts.repository import AccountRepository


@pytest.mark.asyncio
async def test_metric_history_upsert_list_cleanup(tmp_path):
    repo = AccountRepository(str(tmp_path / "h.sqlite3"))
    try:
        await repo.connect()
        await repo.migrate()
        await repo.upsert_metric_history(
            provider="codebuddy",
            account_id="cb-1",
            metric_kind="points",
            value={"total_remaining": 100},
            observed_at="2026-08-03T00:00:00+00:00",
        )
        await repo.upsert_metric_history(
            provider="codebuddy",
            account_id="cb-1",
            metric_kind="points",
            value={"total_remaining": 90},
            observed_at="2026-08-03T00:15:00+00:00",
        )
        rows = await repo.list_metric_history(
            provider="codebuddy",
            account_id="cb-1",
            metric_kind="points",
        )
        assert [r["value"]["total_remaining"] for r in rows] == [100, 90]
        assert await repo.delete_metric_history_before("2026-08-03T00:10:00+00:00") == 1
        remaining = await repo.list_metric_history(
            provider="codebuddy",
            account_id="cb-1",
            metric_kind="points",
        )
        assert [r["value"]["total_remaining"] for r in remaining] == [90]
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_metric_history_upsert_is_idempotent(tmp_path):
    repo = AccountRepository(str(tmp_path / "h2.sqlite3"))
    try:
        await repo.connect()
        await repo.migrate()
        observed = "2026-08-03T00:00:00+00:00"
        await repo.upsert_metric_history(
            provider="codebuddy",
            account_id="cb-1",
            metric_kind="points",
            value={"total_remaining": 1},
            observed_at=observed,
        )
        await repo.upsert_metric_history(
            provider="codebuddy",
            account_id="cb-1",
            metric_kind="points",
            value={"total_remaining": 2},
            observed_at=observed,
        )
        rows = await repo.list_metric_history(
            provider="codebuddy",
            account_id="cb-1",
            metric_kind="points",
        )
        assert len(rows) == 1 and rows[0]["value"]["total_remaining"] == 2
    finally:
        await repo.close()
