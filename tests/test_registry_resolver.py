"""Tests for AccountRegistry + CredentialResolver with temp sqlite + vault."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from qb2api.accounts.models import Credential
from qb2api.accounts.registry import AccountRegistry
from qb2api.accounts.repository import AccountRepository
from qb2api.accounts.resolver import CredentialResolver
from qb2api.accounts.vault import CredentialVault


@pytest.fixture
def fernet_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
async def repo(tmp_path):
    db = tmp_path / "reg.sqlite3"
    r = AccountRepository(str(db))
    await r.connect()
    await r.migrate()
    yield r
    await r.close()


@pytest.fixture
def vault(fernet_key: str) -> CredentialVault:
    return CredentialVault(fernet_key)


async def _seed_dynamic(
    repo: AccountRepository,
    vault: CredentialVault,
    *,
    provider: str,
    account_id: str,
    secret: str,
    purpose: str = "chat",
    mode: str = "bearer",
    expires_at: str | None = None,
    has_refresh: bool = False,
    payload: dict | None = None,
) -> None:
    await repo.upsert_account(
        provider=provider,
        account_id=account_id,
        label=account_id,
        source="manual",
        enabled=True,
        masked_identity="***",
    )
    await repo.upsert_purpose(
        provider=provider,
        account_id=account_id,
        purpose=purpose,
        enabled=True,
        status="active",
        verification_status="verified" if purpose == "checkin" else "not_required",
        capabilities=["proxy.chat"] if purpose == "chat" else ["checkin.workbuddy"],
        expires_at=expires_at,
    )
    body = payload or (
        {"pat": secret} if provider == "qoder" and purpose == "chat" else {"access_token": secret}
    )
    if has_refresh and "refresh_token" not in body:
        body = {**body, "refresh_token": "rt-1"}
    blob = vault.encrypt(body)
    await repo.upsert_credential(
        provider=provider,
        account_id=account_id,
        purpose=purpose,
        mode=mode,
        encrypted_payload=blob,
        has_refresh_token=has_refresh,
        expires_at=expires_at,
    )


@pytest.mark.asyncio
async def test_registry_loads_env_slots(repo, vault):
    reg = AccountRegistry(
        repo,
        vault,
        codebuddy_tokens=["ck_aaa", "ck_bbb"],
        qoder_tokens=["pt_one"],
    )
    await reg.rebuild()
    slots = reg.snapshot("chat")
    ids = {(s.provider, s.account_id) for s in slots}
    assert ("codebuddy", "cb-env-0") in ids
    assert ("codebuddy", "cb-env-1") in ids
    assert ("qoder", "qd-env-0") in ids
    views = reg.list_views()
    for v in views:
        # no raw secrets in views
        assert "ck_aaa" not in str(v)
        assert "pt_one" not in str(v)
        assert v.source == "env"


@pytest.mark.asyncio
async def test_registry_merges_db_and_shadows_env(repo, vault):
    secret = "ck_shared_token_value"
    await _seed_dynamic(
        repo, vault, provider="codebuddy", account_id="cb-dyn-1", secret=secret
    )
    reg = AccountRegistry(
        repo,
        vault,
        codebuddy_tokens=[secret, "ck_other"],
        qoder_tokens=[],
    )
    await reg.rebuild()
    slots = reg.snapshot("chat")
    ids = [s.account_id for s in slots]
    # dynamic wins; env-0 shadowed out of pool; env-1 remains
    assert "cb-dyn-1" in ids
    assert "cb-env-0" not in ids
    assert "cb-env-1" in ids

    views = {v.account_id: v for v in reg.list_views()}
    assert views["cb-env-0"].shadowed is True
    assert views["cb-env-1"].shadowed is False
    assert views["cb-dyn-1"].shadowed is False
    assert views["cb-dyn-1"].source == "manual"


@pytest.mark.asyncio
async def test_resolver_env_and_db_credentials(repo, vault):
    await _seed_dynamic(
        repo, vault, provider="codebuddy", account_id="cb-dyn", secret="tok-dyn"
    )
    reg = AccountRegistry(
        repo, vault, codebuddy_tokens=["tok-env"], qoder_tokens=[]
    )
    await reg.rebuild()
    resolver = CredentialResolver(repo, vault, reg, skew_seconds=60)

    env_cred = await resolver.credential("codebuddy", "cb-env-0", "chat")
    assert env_cred.payload["access_token"] == "tok-env"
    assert env_cred.mode == "bearer"

    db_cred = await resolver.credential("codebuddy", "cb-dyn", "chat")
    assert db_cred.payload["access_token"] == "tok-dyn"
    assert db_cred.credential_version == 1

    # cache hit: same object
    again = await resolver.credential("codebuddy", "cb-dyn", "chat")
    assert again is db_cred


@pytest.mark.asyncio
async def test_resolver_skew_triggers_refresh_single_flight(repo, vault):
    soon = (datetime.now(UTC) + timedelta(seconds=30)).replace(microsecond=0)
    exp_iso = soon.isoformat()
    await _seed_dynamic(
        repo,
        vault,
        provider="codebuddy",
        account_id="cb-exp",
        secret="old-access",
        expires_at=exp_iso,
        has_refresh=True,
        payload={"access_token": "old-access", "refresh_token": "rt", "expires_at": exp_iso},
    )
    reg = AccountRegistry(repo, vault, codebuddy_tokens=[], qoder_tokens=[])
    await reg.rebuild()

    calls: list[str] = []
    gate = asyncio.Event()
    started = asyncio.Event()

    async def refresh_cb(provider, account_id, purpose, current: Credential):
        calls.append(current.payload["access_token"])
        started.set()
        await gate.wait()
        new_exp = (datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=0)
        return Credential(
            provider=provider,
            account_id=account_id,
            purpose=purpose,
            mode=current.mode,
            payload={
                "access_token": "new-access",
                "refresh_token": "rt",
                "expires_at": new_exp.isoformat(),
            },
            credential_version=current.credential_version + 1,
            expires_at=new_exp.isoformat(),
            has_refresh_token=True,
        )

    resolver = CredentialResolver(
        repo, vault, reg, skew_seconds=120, refresh_callback=refresh_cb
    )

    # skew=120s, expires in 30s -> needs refresh
    assert resolver.needs_refresh(
        Credential(
            provider="codebuddy",
            account_id="cb-exp",
            purpose="chat",
            mode="bearer",
            payload={"access_token": "x", "expires_at": exp_iso},
            credential_version=1,
            expires_at=exp_iso,
            has_refresh_token=True,
        )
    )

    t1 = asyncio.create_task(resolver.credential("codebuddy", "cb-exp", "chat"))
    await started.wait()
    t2 = asyncio.create_task(resolver.credential("codebuddy", "cb-exp", "chat"))
    # second waiter blocked on same lock; only one refresh
    await asyncio.sleep(0.05)
    gate.set()
    r1, r2 = await asyncio.gather(t1, t2)
    assert r1.payload["access_token"] == "new-access"
    assert r2.payload["access_token"] == "new-access"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_resolver_invalidate_reloads(repo, vault):
    await _seed_dynamic(
        repo, vault, provider="codebuddy", account_id="cb-v", secret="v1"
    )
    reg = AccountRegistry(repo, vault)
    await reg.rebuild()
    resolver = CredentialResolver(repo, vault, reg)
    c1 = await resolver.credential("codebuddy", "cb-v", "chat")
    assert c1.payload["access_token"] == "v1"

    blob = vault.encrypt({"access_token": "v2"})
    await repo.upsert_credential(
        provider="codebuddy",
        account_id="cb-v",
        purpose="chat",
        mode="bearer",
        encrypted_payload=blob,
    )
    # still cached
    c_cached = await resolver.credential("codebuddy", "cb-v", "chat")
    assert c_cached.payload["access_token"] == "v1"

    resolver.invalidate("codebuddy", "cb-v", "chat")
    c2 = await resolver.credential("codebuddy", "cb-v", "chat")
    assert c2.payload["access_token"] == "v2"
    assert c2.credential_version == 2


@pytest.mark.asyncio
async def test_views_never_include_secrets(repo, vault):
    secret = "super-secret-token-xyz"
    await _seed_dynamic(
        repo, vault, provider="codebuddy", account_id="cb-s", secret=secret
    )
    reg = AccountRegistry(repo, vault, codebuddy_tokens=[secret])
    await reg.rebuild()
    for v in reg.list_views():
        dumped = repr(v)
        assert secret not in dumped
        assert "access_token" not in dumped
