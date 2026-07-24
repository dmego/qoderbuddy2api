"""Forward-only schema migration coverage."""

from __future__ import annotations

import sqlite3

import pytest

from qb2api.accounts.repository import AccountRepository


@pytest.mark.asyncio
async def test_v2_usage_rollup_table_gets_token_count_columns(tmp_path):
    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE usage_rollups (
            bucket_start TEXT NOT NULL,
            bucket_kind TEXT NOT NULL,
            provider TEXT NOT NULL,
            account_id TEXT,
            model_id TEXT NOT NULL,
            request_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            latency_p50_ms INTEGER,
            latency_p95_ms INTEGER,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (bucket_start, bucket_kind, provider, account_id, model_id)
        )
        """
    )
    connection.close()
    repository = AccountRepository(str(path))
    await repository.connect()
    await repository.migrate()
    cursor = await repository.db.execute("PRAGMA table_info(usage_rollups)")
    columns = {row[1] for row in await cursor.fetchall()}
    assert {"token_event_count", "missing_token_count"} <= columns
    assert await repository.schema_version() == "4"
    await repository.close()
