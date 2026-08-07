"""Durable admin-session storage contracts."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from qb2api.accounts.repository import AccountRepository
from qb2api.admin.auth import AdminSessionStore, hash_token
from qb2api.config import Settings
from qb2api.runtime import RuntimeServices


@pytest.mark.asyncio
async def test_durable_store_persists_only_session_hashes(tmp_path) -> None:
    repository = AccountRepository(str(tmp_path / "accounts.sqlite3"))
    await repository.connect()
    await repository.migrate()
    store = AdminSessionStore(repository, ttl_hours=12, idle_minutes=60)

    created = await store.create_session()
    rows = await repository.list_admin_sessions()
    assert len(rows) == 1
    assert rows[0]["session_hash"] == hash_token(created["session_id"])
    assert rows[0]["csrf_hash"] == hash_token(created["csrf_token"])
    assert created["session_id"] not in str(rows)
    assert created["csrf_token"] not in str(rows)

    reloaded = AdminSessionStore(repository, ttl_hours=12, idle_minutes=60)
    assert await reloaded.validate_session(created["session_id"]) is not None
    await repository.close()


@pytest.mark.asyncio
async def test_runtime_restart_revokes_existing_admin_sessions(tmp_path) -> None:
    settings = Settings(
        admin_key="admin-secret",
        credential_key=Fernet.generate_key().decode(),
        data_dir=str(tmp_path),
        codebuddy_tokens=[],
        qoder_tokens=[],
    )
    first = await RuntimeServices.start(settings)
    created = await first.admin_sessions.create_session()
    await first.close()

    second = await RuntimeServices.start(settings)
    try:
        assert await second.admin_sessions.validate_session(created["session_id"]) is None
    finally:
        await second.close()
