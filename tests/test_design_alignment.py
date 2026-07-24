"""Critical design-alignment regression tests."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet


def test_env_only_proxy_boots_without_admin_keys(monkeypatch):
    """Design: env-only proxy must not require Admin/Credential keys."""
    import os

    for k in list(os.environ):
        if k.startswith("QB2API_") or k in ("CODEBUDDY_TOKEN", "QODER_TOKEN"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CODEBUDDY_TOKEN", "ck_test_token")
    monkeypatch.setenv("QB2API_ADMIN_UI_ENABLED", "false")
    from qb2api.config import Settings

    s = Settings.from_env(env_file="")
    s.validate_startup()  # must not raise
    assert s.codebuddy_tokens == ["ck_test_token"]
    assert s.admin_key is None


def test_admin_ui_requires_admin_and_credential_keys():
    from qb2api.config import Settings

    s = Settings(
        admin_ui_enabled=True,
        admin_key=None,
        credential_key=None,
        checkin_enabled=False,
    )
    with pytest.raises(ValueError, match="ADMIN_KEY"):
        s.validate_startup()


def test_proxy_and_admin_keys_must_differ():
    from qb2api.config import Settings

    s = Settings(
        admin_ui_enabled=False,
        proxy_api_key="same",
        admin_key="same",
        credential_key=Fernet.generate_key().decode(),
    )
    with pytest.raises(ValueError, match="different"):
        s.validate_startup()


@pytest.mark.asyncio
async def test_checkin_snapshot_requires_verified(tmp_path):
    from qb2api.accounts.registry import AccountRegistry
    from qb2api.accounts.repository import AccountRepository
    from qb2api.accounts.vault import CredentialVault

    key = Fernet.generate_key().decode()
    vault = CredentialVault(key)
    repo = AccountRepository(str(tmp_path / "t.sqlite3"))
    await repo.connect()
    await repo.migrate()
    await repo.upsert_account(
        provider="codebuddy",
        account_id="cb-1",
        label="a",
        source="oauth",
        enabled=True,
    )
    await repo.upsert_purpose(
        provider="codebuddy",
        account_id="cb-1",
        purpose="checkin",
        enabled=True,
        status="active",
        verification_status="unverified",
        capabilities=["checkin.workbuddy"],
    )
    await repo.upsert_credential(
        provider="codebuddy",
        account_id="cb-1",
        purpose="checkin",
        mode="inherit_chat",
        encrypted_payload=vault.encrypt({"access_token": "tok"}),
    )
    reg = AccountRegistry(repo, vault, codebuddy_tokens=[], qoder_tokens=[])
    await reg.rebuild()
    assert reg.snapshot("checkin") == []

    await repo.upsert_purpose(
        provider="codebuddy",
        account_id="cb-1",
        purpose="checkin",
        enabled=True,
        status="active",
        verification_status="verified",
        capabilities=["checkin.workbuddy"],
    )
    await reg.rebuild()
    slots = reg.snapshot("checkin")
    assert len(slots) == 1
    assert slots[0].account_id == "cb-1"
    await repo.close()


@pytest.mark.asyncio
async def test_credential_cas_rejects_stale_version(tmp_path):
    from qb2api.accounts.repository import AccountRepository, CredentialVersionConflict
    from qb2api.accounts.vault import CredentialVault

    key = Fernet.generate_key().decode()
    vault = CredentialVault(key)
    repo = AccountRepository(str(tmp_path / "t.sqlite3"))
    await repo.connect()
    await repo.migrate()
    await repo.upsert_account(
        provider="qoder", account_id="qd-1", label="q", source="manual", enabled=True
    )
    await repo.upsert_credential(
        provider="qoder",
        account_id="qd-1",
        purpose="checkin",
        mode="bearer",
        encrypted_payload=vault.encrypt({"access_token": "a1", "refresh_token": "r1"}),
        has_refresh_token=True,
    )
    row = await repo.get_credential("qoder", "qd-1", "checkin")
    assert row["credential_version"] == 1

    await repo.upsert_credential(
        provider="qoder",
        account_id="qd-1",
        purpose="checkin",
        mode="bearer",
        encrypted_payload=vault.encrypt({"access_token": "a2", "refresh_token": "r1"}),
        has_refresh_token=True,
    )
    with pytest.raises(CredentialVersionConflict):
        await repo.upsert_credential(
            provider="qoder",
            account_id="qd-1",
            purpose="checkin",
            mode="bearer",
            encrypted_payload=vault.encrypt({"access_token": "a3", "refresh_token": "r1"}),
            has_refresh_token=True,
            expected_version=1,
        )
    await repo.close()


@pytest.mark.asyncio
async def test_promote_env_account_creates_durable_id(tmp_path):
    from qb2api.accounts.promote import promote_env_account
    from qb2api.accounts.registry import AccountRegistry
    from qb2api.accounts.repository import AccountRepository
    from qb2api.accounts.vault import CredentialVault

    key = Fernet.generate_key().decode()
    vault = CredentialVault(key)
    repo = AccountRepository(str(tmp_path / "t.sqlite3"))
    await repo.connect()
    await repo.migrate()
    reg = AccountRegistry(repo, vault, codebuddy_tokens=[], qoder_tokens=["pt_env_pat_1"])
    await reg.rebuild()
    assert reg.is_env_account("qoder", "qd-env-0")

    new_id = await promote_env_account(
        reg,
        repo,
        vault,
        provider="qoder",
        account_id="qd-env-0",
        label="main",
    )
    assert new_id != "qd-env-0"
    await reg.rebuild()
    views = {(v.provider, v.account_id): v for v in reg.list_views()}
    assert ("qoder", new_id) in views
    assert views[("qoder", new_id)].source != "env"
    env_view = views[("qoder", "qd-env-0")]
    assert env_view.shadowed is True
    await repo.close()
