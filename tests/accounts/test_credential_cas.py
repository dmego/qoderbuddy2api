"""Credential compare-and-swap contracts."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from cryptography.fernet import Fernet

from qb2api.accounts.repository import (
    AccountRepository,
    CredentialVersionConflict,
)
from qb2api.accounts.vault import CredentialVault


class _Cursor:
    def __init__(self, *, rowcount: int, row: Any = None) -> None:
        self.rowcount = rowcount
        self._row = row

    async def fetchone(self) -> Any:
        return self._row


class _CasLostConnection:
    """Pretend another writer won after the repository read version 1."""

    async def execute(self, query: str, _params: tuple[Any, ...]) -> _Cursor:
        if query.lstrip().startswith("UPDATE credentials"):
            return _Cursor(rowcount=0)
        if query.lstrip().startswith("SELECT * FROM credentials"):
            return _Cursor(rowcount=-1, row={"credential_version": 1})
        raise AssertionError(f"unexpected SQL: {query}")

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


@pytest.mark.asyncio
async def test_zero_row_update_is_credential_version_conflict() -> None:
    repository = AccountRepository(":memory:")
    repository._db = _CasLostConnection()  # type: ignore[assignment]

    with pytest.raises(CredentialVersionConflict):
        await repository.upsert_credential(
            provider="qoder",
            account_id="qd-main",
            purpose="checkin",
            mode="access_refresh",
            encrypted_payload="ciphertext",
            expected_version=1,
        )


def _synchronize_first_read(
    read: Callable[[str, str, str], Awaitable[dict[str, Any] | None]],
    barrier: asyncio.Barrier,
) -> Callable[[str, str, str], Awaitable[dict[str, Any] | None]]:
    async def synchronized(
        provider: str,
        account_id: str,
        purpose: str,
    ) -> dict[str, Any] | None:
        row = await read(provider, account_id, purpose)
        await barrier.wait()
        return row

    return synchronized


async def _update_token(
    repository: AccountRepository,
    vault: CredentialVault,
    token: str,
) -> int:
    return await repository.upsert_credential(
        provider="qoder",
        account_id="qd-main",
        purpose="checkin",
        mode="access_refresh",
        encrypted_payload=vault.encrypt(
            {"access_token": token, "refresh_token": "refresh-v1"}
        ),
        has_refresh_token=True,
        expected_version=1,
    )


@pytest.mark.asyncio
async def test_concurrent_credential_cas_has_exactly_one_winner(tmp_path) -> None:
    path = tmp_path / "accounts.sqlite3"
    first = AccountRepository(str(path))
    second = AccountRepository(str(path))
    await first.connect()
    await first.migrate()
    await second.connect()
    vault = CredentialVault(Fernet.generate_key().decode())
    await first.upsert_account(
        provider="qoder",
        account_id="qd-main",
        label="main",
        source="manual",
        enabled=True,
    )
    await first.upsert_credential(
        provider="qoder",
        account_id="qd-main",
        purpose="checkin",
        mode="access_refresh",
        encrypted_payload=vault.encrypt({"access_token": "v1"}),
    )
    barrier = asyncio.Barrier(2)
    first.get_credential = _synchronize_first_read(  # type: ignore[method-assign]
        first.get_credential, barrier
    )
    second.get_credential = _synchronize_first_read(  # type: ignore[method-assign]
        second.get_credential, barrier
    )

    try:
        outcomes = await asyncio.wait_for(
            asyncio.gather(
                _update_token(first, vault, "access-a"),
                _update_token(second, vault, "access-b"),
                return_exceptions=True,
            ),
            timeout=3,
        )
    finally:
        await first.close()
        await second.close()

    assert sum(isinstance(value, int) for value in outcomes) == 1
    assert sum(isinstance(value, CredentialVersionConflict) for value in outcomes) == 1
