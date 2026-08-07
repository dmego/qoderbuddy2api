"""Usage rollup accuracy, token provenance, and retention."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from qb2api.accounts.repository import AccountRepository
from qb2api.config import Settings
from qb2api.control.telemetry import UsageRollupService


@pytest.mark.asyncio
async def test_rollup_counts_known_and_missing_tokens(tmp_path):
    repository = AccountRepository(str(tmp_path / "usage.sqlite3"))
    await repository.connect()
    await repository.migrate()
    now = datetime(2026, 7, 23, 12, 34, 30, tzinfo=UTC)
    await repository.add_request_events(
        [
            _event("e-1", now, status="succeeded", input_tokens=10, output_tokens=5, latency_ms=100),
            _event("e-2", now, status="failed", latency_ms=300),
        ]
    )
    service = UsageRollupService(
        settings=Settings(usage_detail_retention_days=90),
        repository=repository,
    )
    result = await service.rollup_once(now)
    assert result["groups"] == 3
    minute = (await repository.list_usage_rollups(bucket_kind="minute"))[0]
    assert minute["request_count"] == 2
    assert minute["success_count"] == 1
    assert minute["error_count"] == 1
    assert minute["input_tokens"] == 10
    assert minute["output_tokens"] == 5
    assert minute["token_event_count"] == 1
    assert minute["missing_token_count"] == 1
    assert minute["latency_p50_ms"] == 100
    assert minute["latency_p95_ms"] == 300
    await repository.close()


@pytest.mark.asyncio
async def test_rollup_prunes_only_expired_detail_events(tmp_path):
    repository = AccountRepository(str(tmp_path / "retention.sqlite3"))
    await repository.connect()
    await repository.migrate()
    now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    await repository.add_request_events(
        [
            _event("old", now - timedelta(days=2), status="succeeded"),
            _event("new", now, status="succeeded"),
        ]
    )
    service = UsageRollupService(
        settings=Settings(usage_detail_retention_days=1),
        repository=repository,
    )
    result = await service.rollup_once(now)
    assert result["deleted_events"] == 1
    events = await repository.list_request_events(limit=10)
    assert [event["event_id"] for event in events] == ["new"]
    await repository.close()


def _event(event_id: str, started_at: datetime, **values):
    return {
        "event_id": event_id,
        "request_id": f"request-{event_id}",
        "provider": "qoder",
        "account_id": "qd-1",
        "model_id": "model-a",
        "protocol": "openai",
        "status": values.pop("status"),
        "started_at": started_at.isoformat(),
        **values,
    }
