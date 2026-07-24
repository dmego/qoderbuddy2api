"""Atomic account promotion and credential import contracts."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from qb2api.accounts.imports import persist_qoder_checkin
from qb2api.accounts.promote import promote_env_account
from qb2api.accounts.registry import AccountRegistry
from qb2api.accounts.repository import AccountRepository
from qb2api.accounts.vault import CredentialVault


@pytest.mark.asyncio
async def test_promotion_failure_leaves_no_partial_durable_account(
    tmp_path,
) -> None:
    repository = AccountRepository(str(tmp_path / "accounts.sqlite3"))
    await repository.connect()
    await repository.migrate()
    vault = CredentialVault(Fernet.generate_key().decode())
    registry = AccountRegistry(
        repository,
        vault,
        codebuddy_tokens=["ck-env-token"],
        qoder_tokens=[],
    )
    await registry.rebuild()

    async def fail_credential_write(**_values) -> int:
        raise RuntimeError("injected credential failure")

    repository.upsert_credential = fail_credential_write  # type: ignore[method-assign]

    try:
        with pytest.raises(RuntimeError, match="injected credential failure"):
            await promote_env_account(
                registry,
                repository,
                vault,
                provider="codebuddy",
                account_id="cb-env-0",
                durable_id="cb-promoted",
            )

        assert await repository.list_accounts() == []
        assert registry.env_secret("codebuddy", "cb-env-0", "chat") == "ck-env-token"
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_qoder_checkin_import_failure_preserves_needs_import(tmp_path) -> None:
    repository = AccountRepository(str(tmp_path / "accounts.sqlite3"))
    await repository.connect()
    await repository.migrate()
    vault = CredentialVault(Fernet.generate_key().decode())
    await repository.upsert_account(
        provider="qoder",
        account_id="qd-main",
        label="main",
        source="manual",
        enabled=True,
    )
    await repository.upsert_purpose(
        provider="qoder",
        account_id="qd-main",
        purpose="checkin",
        enabled=False,
        status="needs_import",
        verification_status="unverified",
        capabilities=["checkin.qoder"],
    )

    async def fail_credential_write(**_values) -> int:
        raise RuntimeError("injected credential failure")

    repository.upsert_credential = fail_credential_write  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="injected credential failure"):
            await persist_qoder_checkin(
                repository,
                vault,
                account_id="qd-main",
                access_token="access",
                refresh_token="refresh",
                verified_at="2026-07-22T00:00:00+00:00",
            )
        purposes = await repository.list_purposes("qoder", "qd-main")
        assert purposes[0]["status"] == "needs_import"
        assert purposes[0]["verification_status"] == "unverified"
        assert await repository.get_credential("qoder", "qd-main", "checkin") is None
    finally:
        await repository.close()
