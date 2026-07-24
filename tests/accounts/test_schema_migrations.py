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


@pytest.mark.asyncio
async def test_v3_to_v4_adds_management_tables_and_preserves_data(tmp_path):
    path = tmp_path / "v3.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta VALUES ('schema_version', '3');
        CREATE TABLE accounts (
            provider TEXT NOT NULL, account_id TEXT NOT NULL, label TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'manual', enabled INTEGER NOT NULL DEFAULT 1,
            masked_identity TEXT, identity_hash TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY (provider, account_id)
        );
        INSERT INTO accounts VALUES (
            'qoder', 'preserved', 'Preserved', 'manual', 1, NULL, NULL,
            '2026-07-23T00:00:00+00:00', '2026-07-23T00:00:00+00:00'
        );
        CREATE TABLE service_events (
            cursor INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
            service_name TEXT NOT NULL, event_type TEXT NOT NULL, action TEXT,
            desired_state TEXT, observed_state TEXT, operation_id TEXT, status TEXT,
            error_code TEXT, created_at TEXT NOT NULL
        );
        INSERT INTO service_events (
            event_id, service_name, event_type, status, created_at
        ) VALUES ('event-v3', 'proxy-worker', 'operation', 'succeeded',
                  '2026-07-23T00:00:00+00:00');
        CREATE TABLE audit_events (
            event_id TEXT PRIMARY KEY, actor_type TEXT NOT NULL, actor_id TEXT,
            action TEXT NOT NULL, resource_type TEXT NOT NULL, resource_id TEXT,
            result TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        INSERT INTO audit_events VALUES (
            'audit-v3', 'admin', NULL, 'account.update', 'account', 'qoder:preserved',
            'succeeded', '{}', '2026-07-23T00:00:00+00:00'
        );
        CREATE TABLE request_events (
            event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, provider TEXT NOT NULL,
            account_id TEXT, model_id TEXT NOT NULL, protocol TEXT NOT NULL,
            status TEXT NOT NULL, http_status INTEGER, input_tokens INTEGER,
            output_tokens INTEGER, latency_ms INTEGER,
            stream_committed INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL,
            finished_at TEXT, error_code TEXT, redacted_error TEXT
        );
        INSERT INTO request_events VALUES (
            'usage-v3', 'request-v3', 'qoder', 'preserved', 'model-v3', 'openai',
            'succeeded', 200, 1, 2, 3, 0, '2026-07-23T00:00:00+00:00',
            '2026-07-23T00:00:01+00:00', NULL, NULL
        );
        """
    )
    connection.close()

    repository = AccountRepository(str(path))
    await repository.connect()
    await repository.migrate()
    await repository.migrate()

    tables = await _names(repository, "table")
    indexes = await _names(repository, "index")
    columns = {
        row[1]
        for row in await (await repository.db.execute(
            "PRAGMA table_info(service_events)"
        )).fetchall()
    }
    account = await repository.db.execute_fetchall(
        "SELECT label FROM accounts WHERE account_id='preserved'"
    )
    event = await repository.db.execute_fetchall(
        "SELECT event_id, status, in_flight FROM service_events WHERE event_id='event-v3'"
    )
    audit = await repository.db.execute_fetchall(
        "SELECT action, result FROM audit_events WHERE event_id='audit-v3'"
    )
    usage = await repository.db.execute_fetchall(
        "SELECT request_id, input_tokens, output_tokens FROM request_events "
        "WHERE event_id='usage-v3'"
    )

    assert {"service_events", "metric_refresh_operations"} <= tables
    assert "idx_service_events_cursor" in indexes
    assert "in_flight" in columns
    assert account[0][0] == "Preserved"
    assert tuple(event[0]) == ("event-v3", "succeeded", None)
    assert tuple(audit[0]) == ("account.update", "succeeded")
    assert tuple(usage[0]) == ("request-v3", 1, 2)
    assert await repository.schema_version() == "4"
    await repository.close()


async def _names(repository: AccountRepository, kind: str) -> set[str]:
    rows = await repository.db.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type=?", (kind,)
    )
    return {str(row[0]) for row in rows}
