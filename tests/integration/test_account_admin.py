"""HTTP contracts for atomic, redacted account imports."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx
import pytest

from qb2api.accounts.imports import persist_qoder_chat, persist_qoder_checkin
from qb2api.accounts.repo_credentials import CredentialVersionConflict
from qb2api.admin import account_routes, import_routes, import_support
from qb2api.checkin.models import CheckInOutcome, CheckInResult
from qb2api.config import Settings
from qb2api.providers.qoder_auth import QoderError


class _QoderProbe:
    def __init__(self, result: CheckInResult) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def status(self, **values: object) -> CheckInResult:
        self.calls.append(values)
        return self.result


class _WorkBuddyProbe:
    def __init__(self, result: CheckInResult) -> None:
        self.result = result
        self.checkin_calls: list[dict[str, object]] = []
        self.status_calls: list[dict[str, object]] = []
        self.closed = False

    async def checkin(self, **values: object) -> CheckInResult:
        self.checkin_calls.append(values)
        return self.result

    async def status(self, **values: object) -> CheckInResult:
        self.status_calls.append(values)
        return self.result

    async def close(self) -> None:
        self.closed = True


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret"}


async def _post_qoder_checkin(app, account_id: str) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        return await client.post(
            "/api/admin/auth/qoder/checkin",
            headers=_headers(),
            json={"account_id": account_id, "access_token": "access-secret", "refresh_token": "refresh-secret"},
        )


async def _post_qoder_chat(
    app,
    *,
    pat: str,
    account_id: str | None = None,
) -> httpx.Response:
    body = {"label": "qoder", "pat": pat}
    if account_id is not None:
        body["account_id"] = account_id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        return await client.post(
            "/api/admin/auth/qoder/chat",
            headers=_headers(),
            json=body,
        )


@pytest.mark.asyncio
async def test_qoder_import_rejects_unknown_probe_without_mutation(admin_context) -> None:
    app, repository, vault, registry = admin_context
    account_id = await persist_qoder_chat(repository, vault, label="main", pat="pat-secret")
    await registry.rebuild()
    app.state.checkin_service = SimpleNamespace(qoder_client=_QoderProbe(CheckInResult(
        outcome=CheckInOutcome.FAILED, provider="qoder", raw_status="UNKNOWN",
    )))

    response = await _post_qoder_checkin(app, account_id)

    assert response.status_code == 400
    purposes = await repository.list_purposes("qoder", account_id)
    checkin = next(item for item in purposes if item["purpose"] == "checkin")
    assert checkin["status"] == "needs_import"
    assert await repository.get_credential("qoder", account_id, "checkin") is None


@pytest.mark.asyncio
async def test_qoder_import_commits_verified_credential_and_redacts_response(admin_context) -> None:
    app, repository, vault, registry = admin_context
    account_id = await persist_qoder_chat(repository, vault, label="main", pat="pat-secret")
    await registry.rebuild()
    app.state.checkin_service = SimpleNamespace(qoder_client=_QoderProbe(CheckInResult(
        outcome=CheckInOutcome.SKIPPED, provider="qoder", raw_status="CLAIMABLE",
    )))

    response = await _post_qoder_checkin(app, account_id)

    assert response.status_code == 200
    assert "access-secret" not in response.text
    assert "refresh-secret" not in response.text
    purposes = await repository.list_purposes("qoder", account_id)
    checkin = next(item for item in purposes if item["purpose"] == "checkin")
    assert checkin["status"] == "active"
    assert checkin["verification_status"] == "verified"
    assert await repository.get_credential("qoder", account_id, "checkin") is not None


@pytest.mark.asyncio
async def test_codebuddy_manual_import_does_not_enable_unverified_checkin(admin_context) -> None:
    app, repository, _vault, _registry = admin_context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        response = await client.post(
            "/api/admin/auth/codebuddy/manual", headers=_headers(),
            json={"label": "manual", "access_token": "bearer-secret"},
        )

    assert response.status_code == 200
    assert "bearer-secret" not in response.text
    account_id = response.json()["account"]["account_id"]
    purposes = await repository.list_purposes("codebuddy", account_id)
    checkin = next(item for item in purposes if item["purpose"] == "checkin")
    assert checkin["enabled"] is False
    assert checkin["verification_status"] == "unverified"
    assert await repository.get_credential("codebuddy", account_id, "checkin") is None
    actions = {
        item["action"] for item in await repository.list_audit_events()
        if item["resource_id"] and account_id in item["resource_id"]
    }
    assert {"account.import", "credential.import"} <= actions


@pytest.mark.asyncio
async def test_codebuddy_manual_import_skips_auto_claim_when_checkin_is_disabled(
    admin_context,
    monkeypatch,
) -> None:
    app, repository, _vault, _registry = admin_context
    probe = _WorkBuddyProbe(
        CheckInResult(outcome=CheckInOutcome.ALREADY_CHECKED_IN, provider="codebuddy")
    )
    monkeypatch.setattr(import_support, "workbuddy_client", lambda _settings: probe)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        response = await client.post(
            "/api/admin/auth/codebuddy/manual",
            headers=_headers(),
            json={"label": "manual", "access_token": "bearer-secret"},
        )

    assert response.status_code == 200
    assert response.json()["checkin_verified"] is False
    assert probe.checkin_calls == []
    assert probe.status_calls == []
    account_id = response.json()["account"]["account_id"]
    purposes = await repository.list_purposes("codebuddy", account_id)
    checkin = next(item for item in purposes if item["purpose"] == "checkin")
    assert checkin["enabled"] is False


@pytest.mark.asyncio
async def test_codebuddy_auto_verification_uses_status_without_claim(
    admin_context,
    monkeypatch,
) -> None:
    app, repository, _vault, _registry = admin_context
    app.state.settings = Settings(
        admin_key="admin-secret",
        codebuddy_checkin_enabled=True,
        codebuddy_checkin_status_method="GET",
    )
    probe = _WorkBuddyProbe(
        CheckInResult(outcome=CheckInOutcome.ALREADY_CHECKED_IN, provider="codebuddy")
    )
    monkeypatch.setattr(import_support, "workbuddy_client", lambda _settings: probe)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        response = await client.post(
            "/api/admin/auth/codebuddy/manual",
            headers=_headers(),
            json={"label": "manual", "access_token": "bearer-secret"},
        )

    assert response.status_code == 200
    assert response.json()["checkin_verified"] is True
    assert probe.checkin_calls == []
    assert probe.status_calls == [{"auth_mode": "bearer", "access_token": "bearer-secret"}]
    assert probe.closed is True
    account_id = response.json()["account"]["account_id"]
    purposes = await repository.list_purposes("codebuddy", account_id)
    checkin = next(item for item in purposes if item["purpose"] == "checkin")
    assert checkin["enabled"] is True


@pytest.mark.asyncio
async def test_qoder_chat_import_does_not_persist_derived_token_when_status_rejects(
    admin_context,
    monkeypatch,
) -> None:
    app, repository, _vault, _registry = admin_context
    probe = _QoderProbe(
        CheckInResult(outcome=CheckInOutcome.FAILED, provider="qoder", raw_status="UNKNOWN")
    )
    app.state.checkin_service = SimpleNamespace(qoder_client=probe)

    async def derive(_settings: Settings, _pat: str) -> tuple[str, str]:
        return "derived-access", "derived-refresh"

    monkeypatch.setattr(import_routes, "derive_qoder_checkin", derive)

    response = await _post_qoder_chat(app, pat="pat-secret")

    assert response.status_code == 200
    assert response.json()["checkin_derived"] is False
    assert probe.calls == [{"access_token": "derived-access", "account_id": response.json()["account"]["account_id"]}]
    account_id = response.json()["account"]["account_id"]
    purposes = await repository.list_purposes("qoder", account_id)
    checkin = next(item for item in purposes if item["purpose"] == "checkin")
    assert checkin["status"] == "needs_import"
    assert await repository.get_credential("qoder", account_id, "checkin") is None


@pytest.mark.asyncio
async def test_qoder_chat_reauthorization_preserves_existing_checkin_credential(
    admin_context,
    monkeypatch,
) -> None:
    app, repository, vault, registry = admin_context
    account_id = await persist_qoder_chat(repository, vault, label="main", pat="old-pat")
    await persist_qoder_checkin(
        repository,
        vault,
        account_id=account_id,
        access_token="old-checkin-access",
        refresh_token="old-checkin-refresh",
        verified_at="2026-07-30T00:00:00+00:00",
    )
    await registry.rebuild()

    async def derive(_settings: Settings, _pat: str) -> tuple[str, str]:
        return "new-checkin-access", "new-checkin-refresh"

    monkeypatch.setattr(import_routes, "derive_qoder_checkin", derive)

    response = await _post_qoder_chat(app, pat="new-pat", account_id=account_id)

    assert response.status_code == 200
    assert response.json()["checkin_derived"] is False
    credential = await repository.get_credential("qoder", account_id, "checkin")
    assert credential is not None
    assert credential["credential_version"] == 1
    assert vault.decrypt(credential["encrypted_payload"])["access_token"] == "old-checkin-access"


@pytest.mark.asyncio
async def test_qoder_rederive_rejects_unverified_derived_token(
    admin_context,
    monkeypatch,
) -> None:
    app, repository, vault, registry = admin_context
    account_id = await persist_qoder_chat(repository, vault, label="main", pat="pat-secret")
    await registry.rebuild()
    app.state.checkin_service = SimpleNamespace(
        qoder_client=_QoderProbe(CheckInResult(outcome=CheckInOutcome.FAILED, provider="qoder"))
    )

    async def derive(_settings: Settings, _pat: str) -> tuple[str, str]:
        return "derived-access", "derived-refresh"

    monkeypatch.setattr(account_routes, "derive_qoder_checkin", derive)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        response = await client.post(
            f"/api/admin/accounts/qoder/{account_id}/rederive-checkin",
            headers=_headers(),
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "checkin_credential_rejected"
    assert await repository.get_credential("qoder", account_id, "checkin") is None


@pytest.mark.asyncio
async def test_qoder_rederive_rejects_environment_account_before_deriving(
    admin_context,
    monkeypatch,
) -> None:
    app, _repository, _vault, registry = admin_context
    registry.set_env_tokens(qoder_tokens=["env-pat"])
    await registry.rebuild()

    async def derive(_settings: Settings, _pat: str) -> tuple[str, str]:
        return "derived-access", "derived-refresh"

    monkeypatch.setattr(account_routes, "derive_qoder_checkin", derive)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://test",
    ) as client:
        response = await client.post(
            "/api/admin/accounts/qoder/qd-env-0/rederive-checkin",
            headers=_headers(),
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "cannot_modify_env_account"


@pytest.mark.asyncio
async def test_verify_qoder_checkin_accepts_claimed_today_and_rejects_failure(
    admin_context,
) -> None:
    """verify_qoder_checkin 应接受任何非失败状态（CLAIMED/ALREADY/SKIPPED），拒绝失败。"""
    app, _repository, _vault, _registry = admin_context

    def make_probe(outcome: CheckInOutcome) -> SimpleNamespace:
        return SimpleNamespace(qoder_client=_QoderProbe(
            CheckInResult(outcome=outcome, provider="qoder")
        ))

    state = SimpleNamespace(checkin_service=None)
    accepted = {CheckInOutcome.CLAIMED, CheckInOutcome.ALREADY_CHECKED_IN, CheckInOutcome.SKIPPED}
    rejected = {
        CheckInOutcome.AUTH_FAILED,
        CheckInOutcome.NEEDS_REAUTH,
        CheckInOutcome.FAILED,
        CheckInOutcome.RATE_LIMITED,
        CheckInOutcome.TRANSIENT_ERROR,
    }
    for outcome in accepted:
        state.checkin_service = make_probe(outcome)
        assert await import_support.verify_qoder_checkin(state, "acct", "tok") is True, outcome
    for outcome in rejected:
        state.checkin_service = make_probe(outcome)
        assert await import_support.verify_qoder_checkin(state, "acct", "tok") is False, outcome


@pytest.mark.asyncio
async def test_qoder_checkin_persistence_rejects_stale_credential_version(admin_context) -> None:
    _app, repository, vault, _registry = admin_context
    account_id = await persist_qoder_chat(repository, vault, label="main", pat="pat-secret")
    await persist_qoder_checkin(
        repository,
        vault,
        account_id=account_id,
        access_token="old-access",
        refresh_token="old-refresh",
        verified_at="2026-07-30T00:00:00+00:00",
    )

    with pytest.raises(CredentialVersionConflict):
        await persist_qoder_checkin(
            repository,
            vault,
            account_id=account_id,
            access_token="new-access",
            refresh_token="new-refresh",
            verified_at="2026-07-30T00:00:00+00:00",
            expected_version=0,
        )

    credential = await repository.get_credential("qoder", account_id, "checkin")
    assert credential is not None
    assert vault.decrypt(credential["encrypted_payload"])["access_token"] == "old-access"


@pytest.mark.asyncio
async def test_qoder_derive_never_logs_upstream_response_message(monkeypatch, caplog) -> None:
    class Session:
        refresh_token = "refresh-secret"
        security_oauth_token = "oauth-secret"

        def __init__(self, _pat: str) -> None:
            pass

        async def authenticate(self) -> None:
            return None

        async def close(self) -> None:
            return None

    monkeypatch.setattr(import_support, "QoderSession", Session)

    with caplog.at_level(logging.WARNING, logger="qb2api.admin.import_support"):
        result = await import_support.derive_qoder_checkin(
            Settings(admin_key="admin-secret"),
            "pat-secret-must-not-leak",
        )

    # securityOauthToken 直接作为 access_token 派生，无需 deviceToken/refresh
    assert result == ("oauth-secret", "refresh-secret")
    assert "pat-secret-must-not-leak" not in caplog.text


@pytest.mark.asyncio
async def test_qoder_derive_returns_none_when_authenticate_fails(monkeypatch) -> None:
    class Session:
        refresh_token = ""
        security_oauth_token = ""

        def __init__(self, _pat: str) -> None:
            pass

        async def authenticate(self) -> None:
            raise QoderError("auth failed", status_code=401)

        async def close(self) -> None:
            return None

    monkeypatch.setattr(import_support, "QoderSession", Session)

    result = await import_support.derive_qoder_checkin(
        Settings(admin_key="admin-secret"),
        "pat-secret",
    )
    assert result is None


@pytest.mark.asyncio
async def test_qoder_derive_returns_none_without_security_oauth_token(monkeypatch) -> None:
    class Session:
        refresh_token = "refresh-secret"
        security_oauth_token = ""  # 上游未下发 jt-

        def __init__(self, _pat: str) -> None:
            pass

        async def authenticate(self) -> None:
            return None

        async def close(self) -> None:
            return None

    monkeypatch.setattr(import_support, "QoderSession", Session)

    result = await import_support.derive_qoder_checkin(
        Settings(admin_key="admin-secret"),
        "pat-secret",
    )
    assert result is None
