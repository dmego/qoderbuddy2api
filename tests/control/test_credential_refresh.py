"""Tests for proactive rotation of short-lived credentials."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from qb2api.accounts.models import Credential
from qb2api.accounts.refresh import REFRESHABLE_PROVIDERS, BearerRefreshExecutor
from qb2api.auth.orcaterm import OrcaTermAuthClient, OrcaTermAuthResult
from qb2api.config import Settings
from qb2api.control.credential_refresh_scheduler import CredentialRefreshScheduler


class _Result:
    def __init__(self, token: str | None, expires_in: int | None = 7200, refresh_token=None):
        self.status = "success" if token else "error"
        self.access_token = token
        self.expires_in = expires_in
        self.refresh_token = refresh_token
        self.message = None


class _Repo:
    """Captures the upsert and replays the stored credential."""

    def __init__(self, metadata: list[dict] | None = None) -> None:
        self.upserts: list[dict] = []
        self._metadata = metadata or []

    async def upsert_credential(self, **kwargs):
        self.upserts.append(kwargs)
        return kwargs["expected_version"] + 1

    async def list_credential_metadata(self, provider: str | None = None):
        return [row for row in self._metadata if provider is None or row["provider"] == provider]


class _Vault:
    def encrypt(self, payload: dict) -> str:
        return "enc:" + ",".join(sorted(payload))

    def decrypt(self, blob: str) -> dict:
        return {}


def _orcaterm_credential(version: int = 1) -> Credential:
    return Credential(
        provider="orcaterm",
        account_id="oct-1",
        purpose="chat",
        mode="bearer",
        payload={"access_token": "old-token"},
        credential_version=version,
        expires_at="2026-09-11T12:00:00+00:00",
        has_refresh_token=False,
    )


class _OrcaClient:
    def __init__(self, token: str | None = "new-token") -> None:
        self.calls: list[str] = []
        self._token = token

    async def refresh(self, access_token: str) -> OrcaTermAuthResult:
        self.calls.append(access_token)
        return OrcaTermAuthResult(
            status="success" if self._token else "error",
            access_token=self._token,
            expires_in=7200 if self._token else None,
            message=None if self._token else "TOKEN_EXPIRED",
        )


@pytest.mark.asyncio
async def test_orcaterm_refresh_persists_rotated_token():
    """The access token is its own refresh credential and must be persisted."""
    repo, client = _Repo(), _OrcaClient()
    executor = BearerRefreshExecutor(
        repository=repo, vault=_Vault(), intl_client=None, orcaterm_client=client
    )

    rotated = await executor(_orcaterm_credential())

    assert client.calls == ["old-token"]
    assert rotated is not None
    assert rotated.payload["access_token"] == "new-token"
    assert rotated.credential_version == 2
    assert repo.upserts[0]["mode"] == "bearer"
    # No separate refresh token exists; the flag must stay false rather than
    # claiming one that the console never issued.
    assert repo.upserts[0]["has_refresh_token"] is False


@pytest.mark.asyncio
async def test_orcaterm_refresh_expiry_returns_none_so_stored_token_survives():
    """An expired token cannot be rotated; the resolver keeps what it had."""
    repo = _Repo()
    executor = BearerRefreshExecutor(
        repository=repo, vault=_Vault(), intl_client=None, orcaterm_client=_OrcaClient(token=None)
    )

    assert await executor(_orcaterm_credential()) is None
    assert repo.upserts == []


@pytest.mark.asyncio
async def test_orcaterm_is_refreshable_and_missing_client_is_safe():
    assert "orcaterm" in REFRESHABLE_PROVIDERS
    repo = _Repo()
    executor = BearerRefreshExecutor(repository=repo, vault=_Vault(), intl_client=None)
    assert await executor(_orcaterm_credential()) is None
    assert repo.upserts == []


def _metadata(expires_at: str | None, provider: str = "orcaterm", version: int = 1) -> list[dict]:
    return [
        {
            "provider": provider,
            "account_id": "oct-1",
            "purpose": "chat",
            "credential_version": version,
            "expires_at": expires_at,
        }
    ]


def _resolver(version_after: int | None):
    class _Resolver:
        def __init__(self) -> None:
            self.forced: list[tuple] = []

        async def credential(self, provider, account_id, purpose="chat", *, force_refresh=False):
            self.forced.append((provider, account_id, purpose, force_refresh))
            return Credential(
                provider=provider,
                account_id=account_id,
                purpose=purpose,
                mode="bearer",
                payload={"access_token": "rotated"},
                credential_version=version_after if version_after is not None else 1,
            )

    return _Resolver()


@pytest.mark.asyncio
async def test_scheduler_rotates_only_inside_lead_window():
    soon = (datetime.now(UTC) + timedelta(minutes=5)).replace(microsecond=0).isoformat()
    later = (datetime.now(UTC) + timedelta(hours=4)).replace(microsecond=0).isoformat()
    repo = _Repo(_metadata(soon) + _metadata(later, version=9))
    resolver = _resolver(version_after=2)
    refreshed: list[int] = []

    async def refresh() -> None:
        refreshed.append(1)

    scheduler = CredentialRefreshScheduler(
        settings=Settings(), repo=repo, resolver=resolver, refresh_callback=refresh
    )

    rotated = await scheduler.refresh_once()

    # Only the near-expiry row is forced; the far-future one keeps its version.
    assert rotated == 1
    assert [call[1] for call in resolver.forced] == ["oct-1"]
    assert len(resolver.forced) == 1
    assert refreshed == [1]


@pytest.mark.asyncio
async def test_scheduler_ignores_long_lived_and_unknown_providers():
    """WorkBuddy tokens live a year; rotating them every scan would be waste."""
    soon = (datetime.now(UTC) + timedelta(minutes=5)).replace(microsecond=0).isoformat()
    repo = _Repo(_metadata(soon, provider="workbuddy_intl") + _metadata(soon, provider="codebuddy"))
    resolver = _resolver(version_after=2)

    scheduler = CredentialRefreshScheduler(settings=Settings(), repo=repo, resolver=resolver)

    assert await scheduler.refresh_once() == 0
    assert resolver.forced == []


@pytest.mark.asyncio
async def test_scheduler_does_not_publish_when_nothing_rotated():
    """A failed rotation leaves the version alone and must not reload the Worker."""
    soon = (datetime.now(UTC) + timedelta(minutes=5)).replace(microsecond=0).isoformat()
    repo = _Repo(_metadata(soon))
    resolver = _resolver(version_after=None)  # resolver fell back to stored
    refreshed: list[int] = []

    async def refresh() -> None:
        refreshed.append(1)

    scheduler = CredentialRefreshScheduler(
        settings=Settings(), repo=repo, resolver=resolver, refresh_callback=refresh
    )

    assert await scheduler.refresh_once() == 0
    assert refreshed == []


@pytest.mark.asyncio
async def test_scheduler_interval_and_lead_have_safe_floors():
    scheduler = CredentialRefreshScheduler(
        settings=Settings(credential_refresh_interval_seconds=0, credential_refresh_lead_seconds=-5),
        repo=_Repo(),
        resolver=_resolver(2),
    )

    assert scheduler.interval_seconds >= 60
    assert scheduler.lead_seconds == 0


@pytest.mark.asyncio
async def test_scheduler_start_is_noop_when_disabled():
    scheduler = CredentialRefreshScheduler(
        settings=Settings(credential_refresh_enabled=False), repo=_Repo(), resolver=_resolver(2)
    )

    scheduler.start()
    assert scheduler._task is None
    await scheduler.stop()


@pytest.mark.asyncio
async def test_orcaterm_client_refresh_maps_console_payload(monkeypatch):
    """The client maps OAuthRefreshToken's payload onto our result type."""
    client = OrcaTermAuthClient()

    async def fake_cgi(action: str, data: dict) -> dict:
        assert action == "OAuthRefreshToken"
        assert data == {"accessToken": "old"}
        return {"Response": {"AccessToken": "fresh", "ExpiresIn": 7200}}

    monkeypatch.setattr(client, "_cgi", fake_cgi)
    result = await client.refresh("old")
    await client.close()

    assert result.status == "success"
    assert result.access_token == "fresh"
    assert result.expires_in == 7200


@pytest.mark.asyncio
async def test_orcaterm_client_refresh_surfaces_expired_session(monkeypatch):
    client = OrcaTermAuthClient()

    async def fake_cgi(action: str, data: dict) -> dict:
        return {"Response": {"Error": {"Code": "TOKEN_EXPIRED", "Message": "登录会话已过期"}}}

    monkeypatch.setattr(client, "_cgi", fake_cgi)
    result = await client.refresh("stale")
    await client.close()

    assert result.status == "error"
    assert "过期" in (result.message or "")


@pytest.mark.asyncio
async def test_orcaterm_client_refresh_rejects_empty_token():
    client = OrcaTermAuthClient()
    result = await client.refresh("")
    await client.close()
    assert result.status == "error"
