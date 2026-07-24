"""Metrics scheduler behavior and no-fake-zero guarantees."""

from __future__ import annotations

import asyncio

import pytest
from cryptography.fernet import Fernet

from qb2api.accounts.registry import AccountRegistry
from qb2api.accounts.repository import AccountRepository
from qb2api.accounts.resolver import CredentialResolver
from qb2api.accounts.vault import CredentialVault
from qb2api.checkin.metrics import MetricsScheduler
from qb2api.checkin.quota import QoderQuotaClient, QuotaUnavailableError, normalize_quota
from qb2api.config import Settings


@pytest.fixture
async def metric_context(tmp_path):
    repo = AccountRepository(str(tmp_path / "metrics.sqlite3"))
    await repo.connect()
    await repo.migrate()
    vault = CredentialVault(Fernet.generate_key().decode())
    registry = AccountRegistry(repo, vault)
    resolver = CredentialResolver(repo, vault, registry)
    try:
        yield repo, vault, registry, resolver
    finally:
        await repo.close()


async def _seed(repo, vault, provider, account_id, purpose, payload):
    await repo.upsert_account(
        provider=provider,
        account_id=account_id,
        label=account_id,
        source="manual",
        enabled=True,
    )
    await repo.upsert_purpose(
        provider=provider,
        account_id=account_id,
        purpose=purpose,
        enabled=True,
        status="active",
        verification_status="verified" if purpose == "checkin" else "not_required",
    )
    await repo.upsert_credential(
        provider=provider,
        account_id=account_id,
        purpose=purpose,
        mode="access_refresh" if purpose == "checkin" else "bearer",
        encrypted_payload=vault.encrypt(payload),
        has_refresh_token="refresh_token" in payload,
    )


class FakeQuota:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    async def fetch(self, token):
        self.calls += 1
        await asyncio.sleep(0)
        if self.error:
            raise self.error
        return self.result

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_scheduler_keeps_workbuddy_points_unknown(metric_context):
    repo, vault, registry, resolver = metric_context
    await _seed(repo, vault, "codebuddy", "cb-1", "checkin", {"access_token": "cb-token"})
    scheduler = MetricsScheduler(
        settings=Settings(metrics_enabled=False),
        repo=repo,
        registry=registry,
        resolver=resolver,
        qoder_quota=FakeQuota(),
    )
    result = await scheduler.refresh_once()
    points = [row for row in await repo.list_metric_snapshots() if row["metric_kind"] == "points"]
    assert result["unknown"] >= 1
    assert points[0]["value"] is None
    assert points[0]["last_error"] == "protocol_not_verified"
    await scheduler.stop()


@pytest.mark.asyncio
async def test_qoder_quota_is_allowlisted_and_failures_are_stale(metric_context):
    repo, vault, registry, resolver = metric_context
    await _seed(
        repo,
        vault,
        "qoder",
        "qd-1",
        "checkin",
        {"access_token": "qd-token", "refresh_token": "never-returned"},
    )
    quota = FakeQuota({"user_quota": {"remaining": 12}, "user_id": "must-not-exist"})
    scheduler = MetricsScheduler(
        settings=Settings(metrics_enabled=False),
        repo=repo,
        registry=registry,
        resolver=resolver,
        qoder_quota=quota,
    )
    await scheduler.refresh_once()
    snapshot = [row for row in await repo.list_metric_snapshots() if row["metric_kind"] == "quota"][0]
    assert snapshot["value"] == {"user_quota": {"remaining": 12}}
    quota.error = QuotaUnavailableError("http:503")
    await scheduler.refresh_once()
    stale = [row for row in await repo.list_metric_snapshots() if row["metric_kind"] == "quota"][0]
    assert stale["status"] == "stale"
    assert stale["value"] == {"user_quota": {"remaining": 12}}
    await scheduler.stop()


@pytest.mark.asyncio
async def test_refresh_is_single_flight(metric_context):
    repo, vault, registry, resolver = metric_context
    await _seed(repo, vault, "qoder", "qd-1", "checkin", {"access_token": "qd-token"})
    quota = FakeQuota({"user_quota": {"remaining": 8}})
    scheduler = MetricsScheduler(
        settings=Settings(metrics_enabled=False),
        repo=repo,
        registry=registry,
        resolver=resolver,
        qoder_quota=quota,
    )
    await asyncio.gather(scheduler.refresh_once(), scheduler.refresh_once())
    assert quota.calls == 1
    await scheduler.stop()


def test_normalize_quota_drops_identity_and_unknown_fields():
    assert normalize_quota({"userId": "secret", "userQuota": {"remaining": 3, "email": "x"}}) == {
        "user_quota": {"remaining": 3}
    }


@pytest.mark.asyncio
async def test_quota_client_rejects_empty_access_token():
    client = QoderQuotaClient(client=None)
    with pytest.raises(QuotaUnavailableError, match="access credential"):
        await client.fetch("")
    await client.aclose()
