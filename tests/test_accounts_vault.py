"""Tests for CredentialVault and async AccountRepository."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from qb2api.accounts.repository import AccountRepository
from qb2api.accounts.vault import CredentialVault


@pytest.fixture
def fernet_key() -> str:
    return Fernet.generate_key().decode()


def test_vault_roundtrip(fernet_key: str):
    vault = CredentialVault(fernet_key)
    payload = {"access_token": "secret-token", "refresh_token": "r1"}
    blob = vault.encrypt(payload)
    assert "secret-token" not in blob
    assert vault.decrypt(blob) == payload


def test_vault_rejects_bad_key():
    with pytest.raises(ValueError):
        CredentialVault("not-a-fernet-key")


@pytest.mark.asyncio
async def test_repository_account_and_purpose_roundtrip(tmp_path, fernet_key: str):
    db = tmp_path / "t.sqlite3"
    repo = AccountRepository(str(db))
    await repo.connect()
    await repo.migrate()
    await repo.upsert_account(
        provider="codebuddy",
        account_id="cb-alice",
        label="alice",
        source="oauth",
        enabled=True,
        masked_identity="ali***",
        identity_hash=None,
    )
    await repo.upsert_purpose(
        provider="codebuddy",
        account_id="cb-alice",
        purpose="chat",
        enabled=True,
        status="active",
        verification_status="verified",
        capabilities=["proxy.chat"],
    )
    vault = CredentialVault(fernet_key)
    blob = vault.encrypt({"access_token": "tok"})
    await repo.upsert_credential(
        provider="codebuddy",
        account_id="cb-alice",
        purpose="chat",
        mode="bearer",
        encrypted_payload=blob,
        has_refresh_token=False,
        expires_at=None,
        fingerprint_hmac=None,
    )
    accounts = await repo.list_accounts()
    assert len(accounts) == 1
    assert accounts[0]["account_id"] == "cb-alice"
    purposes = await repo.list_purposes("codebuddy", "cb-alice")
    assert purposes[0]["status"] == "active"
    cred = await repo.get_credential("codebuddy", "cb-alice", "chat")
    assert vault.decrypt(cred["encrypted_payload"])["access_token"] == "tok"
    await repo.close()
