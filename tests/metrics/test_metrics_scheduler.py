"""Metrics scheduler behavior and no-fake-zero guarantees."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from qb2api.accounts.registry import AccountRegistry
from qb2api.accounts.repository import AccountRepository
from qb2api.accounts.resolver import CredentialResolver
from qb2api.accounts.vault import CredentialVault
from qb2api.checkin.codebuddy_credits import CodeBuddyCreditsUnavailableError
from qb2api.checkin.metrics import MetricsScheduler
from qb2api.checkin.metrics_providers import _access_token
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


class FakeCredits:
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
async def test_scheduler_collects_workbuddy_points_fresh(metric_context):
    repo, vault, registry, resolver = metric_context
    await _seed(repo, vault, "codebuddy", "cb-1", "checkin", {"access_token": "cb-token"})
    credits = FakeCredits({
        "unit": "credits", "total_remaining": 300, "total_used": 0,
        "total_capacity": 500, "cycle_remaining": 300, "cycle_capacity": 500,
        "package_count": 2, "depleted_packages": 0, "lowest_remaining": 100,
        "expires_at": None,
    })
    scheduler = MetricsScheduler(
        settings=Settings(metrics_enabled=False),
        repo=repo,
        registry=registry,
        resolver=resolver,
        qoder_quota=FakeQuota(),
        codebuddy_credits=credits,
    )
    try:
        result = await scheduler.refresh_once()
        points = [row for row in await repo.list_metric_snapshots() if row["metric_kind"] == "points"]
        assert result["fresh"] >= 1
        assert points[0]["status"] == "fresh"
        assert points[0]["value"]["total_remaining"] == 300
        history = await repo.list_metric_history(
            provider="codebuddy", account_id="cb-1", metric_kind="points",
        )
        assert history and history[-1]["value"]["total_remaining"] == 300
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_scheduler_marks_workbuddy_points_stale_on_failure(metric_context):
    repo, vault, registry, resolver = metric_context
    await _seed(repo, vault, "codebuddy", "cb-1", "checkin", {"access_token": "cb-token"})
    credits = FakeCredits({
        "unit": "credits", "total_remaining": 300, "total_used": 0,
        "total_capacity": 500, "cycle_remaining": 300, "cycle_capacity": 500,
        "package_count": 2, "depleted_packages": 0, "lowest_remaining": 100,
        "expires_at": None,
    })
    scheduler = MetricsScheduler(
        settings=Settings(metrics_enabled=False),
        repo=repo,
        registry=registry,
        resolver=resolver,
        qoder_quota=FakeQuota(),
        codebuddy_credits=credits,
    )
    try:
        await scheduler.refresh_once()
        credits.error = CodeBuddyCreditsUnavailableError("http:503")
        await scheduler.refresh_once()
        points = [row for row in await repo.list_metric_snapshots() if row["metric_kind"] == "points"]
        assert points[0]["status"] == "stale"
        assert points[0]["value"]["total_remaining"] == 300
    finally:
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
async def test_provider_metrics_collect_when_only_chat_purpose_is_enabled(metric_context):
    repo, vault, registry, resolver = metric_context
    await _seed(repo, vault, "qoder", "qd-chat-only", "chat", {"access_token": "qd-token"})
    await _seed(repo, vault, "codebuddy", "cb-chat-only", "chat", {"access_token": "cb-token"})
    quota = FakeQuota({"user_quota": {"remaining": 42, "unit": "credits"}})
    credits = FakeCredits({"total_remaining": 73, "unit": "credits"})
    scheduler = MetricsScheduler(
        settings=Settings(metrics_enabled=False),
        repo=repo,
        registry=registry,
        resolver=resolver,
        qoder_quota=quota,
        codebuddy_credits=credits,
    )
    try:
        result = await scheduler.refresh_once()
        snapshots = [row for row in await repo.list_metric_snapshots() if row["metric_kind"] == "quota"]
        assert result["fresh"] >= 1
        assert snapshots and snapshots[0]["value"] == {"user_quota": {"remaining": 42, "unit": "credits"}}
        assert quota.calls == 1
        points = [row for row in await repo.list_metric_snapshots() if row["metric_kind"] == "points"]
        assert points and points[0]["value"]["total_remaining"] == 73
        assert credits.calls == 1
    finally:
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


@pytest.mark.asyncio
async def test_scheduler_status_exposes_retry_backoff(metric_context):
    repo, vault, registry, resolver = metric_context
    await _seed(repo, vault, "qoder", "qd-1", "checkin", {"access_token": "qd-token"})
    scheduler = MetricsScheduler(
        settings=Settings(metrics_enabled=False),
        repo=repo,
        registry=registry,
        resolver=resolver,
        qoder_quota=FakeQuota(error=QuotaUnavailableError("http:503")),
    )

    await scheduler.refresh_once()

    status = scheduler.status_snapshot()
    assert status["last_result"]["unavailable"] >= 1
    assert status["backoff"][0]["metric"] == "qoder:qd-1:quota"
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


def test_access_token_rejects_pat_without_exchange():
    credential = SimpleNamespace(payload={"pat": "qoder-pat"})
    with pytest.raises(QuotaUnavailableError, match="access token unavailable"):
        _access_token(credential)
