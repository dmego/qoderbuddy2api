"""Atomic durable account and credential import operations."""

from __future__ import annotations

from .promote import new_account_slug
from .repository import AccountRepository
from .vault import CredentialVault


async def persist_codebuddy_account(
    repo: AccountRepository,
    vault: CredentialVault,
    *,
    label: str,
    source: str,
    access_token: str,
    refresh_token: str | None = None,
    expires_at: str | None = None,
) -> str:
    account_id = new_account_slug("codebuddy")
    payload = {"access_token": access_token}
    if refresh_token:
        payload["refresh_token"] = refresh_token
    encrypted = vault.encrypt(payload)
    async with repo.transaction():
        await repo.upsert_account(
            provider="codebuddy",
            account_id=account_id,
            label=label,
            source=source,
            enabled=True,
            masked_identity=_mask(access_token),
        )
        await _write_codebuddy_purposes(repo, account_id, expires_at)
        await repo.upsert_credential(
            provider="codebuddy",
            account_id=account_id,
            purpose="chat",
            mode="oauth" if refresh_token else "bearer",
            encrypted_payload=encrypted,
            has_refresh_token=bool(refresh_token),
            expires_at=expires_at,
        )
    return account_id


async def persist_qoder_chat(
    repo: AccountRepository,
    vault: CredentialVault,
    *,
    label: str,
    pat: str,
    account_id: str | None = None,
) -> str:
    account = await _qoder_account(repo, account_id)
    if account_id is not None and account is None:
        raise LookupError(f"qoder account not found: {account_id}")
    durable_id = account_id or new_account_slug("qoder")
    encrypted = vault.encrypt({"pat": pat})
    async with repo.transaction():
        await repo.upsert_account(
            provider="qoder",
            account_id=durable_id,
            label=label,
            source=(account or {}).get("source") or "manual",
            enabled=True,
            masked_identity=_mask(pat),
            identity_hash=(account or {}).get("identity_hash"),
        )
        await _write_qoder_chat_purpose(repo, durable_id)
        if account is None:
            await _write_qoder_unconfigured_checkin(repo, durable_id)
        await repo.upsert_credential(
            provider="qoder",
            account_id=durable_id,
            purpose="chat",
            mode="pat",
            encrypted_payload=encrypted,
        )
    return durable_id


async def persist_qoder_checkin(
    repo: AccountRepository,
    vault: CredentialVault,
    *,
    account_id: str,
    access_token: str,
    refresh_token: str,
    verified_at: str,
) -> int:
    if await _qoder_account(repo, account_id) is None:
        raise LookupError(f"qoder account not found: {account_id}")
    encrypted = vault.encrypt(
        {"access_token": access_token, "refresh_token": refresh_token}
    )
    async with repo.transaction():
        await repo.upsert_purpose(
            provider="qoder",
            account_id=account_id,
            purpose="checkin",
            enabled=True,
            status="active",
            verification_status="verified",
            capabilities=["checkin.qoder"],
            verified_at=verified_at,
        )
        return await repo.upsert_credential(
            provider="qoder",
            account_id=account_id,
            purpose="checkin",
            mode="access_refresh",
            encrypted_payload=encrypted,
            has_refresh_token=True,
        )


async def _qoder_account(
    repo: AccountRepository,
    account_id: str | None,
) -> dict | None:
    if account_id is None:
        return None
    accounts = await repo.list_accounts("qoder")
    return next((row for row in accounts if row["account_id"] == account_id), None)


async def _write_codebuddy_purposes(
    repo: AccountRepository,
    account_id: str,
    expires_at: str | None,
) -> None:
    await repo.upsert_purpose(
        provider="codebuddy",
        account_id=account_id,
        purpose="chat",
        enabled=True,
        status="active",
        verification_status="not_required",
        capabilities=["proxy.chat"],
        expires_at=expires_at,
    )
    await repo.upsert_purpose(
        provider="codebuddy",
        account_id=account_id,
        purpose="checkin",
        enabled=False,
        status="unconfigured",
        verification_status="unverified",
        capabilities=["checkin.workbuddy"],
        expires_at=expires_at,
    )


async def _write_qoder_chat_purpose(
    repo: AccountRepository,
    account_id: str,
) -> None:
    await repo.upsert_purpose(
        provider="qoder",
        account_id=account_id,
        purpose="chat",
        enabled=True,
        status="active",
        verification_status="not_required",
        capabilities=["proxy.chat"],
    )


async def _write_qoder_unconfigured_checkin(
    repo: AccountRepository,
    account_id: str,
) -> None:
    await repo.upsert_purpose(
        provider="qoder",
        account_id=account_id,
        purpose="checkin",
        enabled=False,
        status="needs_import",
        verification_status="unverified",
        capabilities=["checkin.qoder"],
    )


def _mask(secret: str) -> str:
    return f"{secret[:3]}***{secret[-2:]}" if len(secret) > 8 else "***"
