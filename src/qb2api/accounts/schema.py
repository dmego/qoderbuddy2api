"""SQLite schema and persistence-wide helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from .account_queries import INSERT_CREDENTIAL, UPDATE_CREDENTIAL, UPSERT_ACCOUNT, UPSERT_PURPOSE
from .schema_management import MANAGEMENT_SCHEMA_V4

__all__ = [
    "INSERT_CREDENTIAL",
    "SCHEMA",
    "UPDATE_CREDENTIAL",
    "UPSERT_ACCOUNT",
    "UPSERT_PURPOSE",
    "now_iso",
]


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    enabled INTEGER NOT NULL DEFAULT 1,
    masked_identity TEXT,
    identity_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider, account_id)
);

CREATE TABLE IF NOT EXISTS account_purposes (
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'unconfigured',
    verification_status TEXT NOT NULL DEFAULT 'unverified',
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    verified_at TEXT,
    expires_at TEXT,
    last_success_at TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider, account_id, purpose),
    FOREIGN KEY (provider, account_id)
        REFERENCES accounts(provider, account_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'bearer',
    encrypted_payload TEXT NOT NULL,
    payload_version INTEGER NOT NULL DEFAULT 1,
    credential_version INTEGER NOT NULL DEFAULT 1,
    fingerprint_hmac TEXT,
    expires_at TEXT,
    has_refresh_token INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE (provider, account_id, purpose),
    FOREIGN KEY (provider, account_id)
        REFERENCES accounts(provider, account_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS checkin_runs (
    run_id TEXT PRIMARY KEY,
    local_date TEXT NOT NULL,
    timezone TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    trigger TEXT NOT NULL DEFAULT 'scheduler',
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS checkin_attempts (
    run_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    outcome TEXT,
    http_status INTEGER,
    business_code TEXT,
    request_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    redacted_error TEXT,
    PRIMARY KEY (run_id, provider, account_id),
    FOREIGN KEY (run_id) REFERENCES checkin_runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS checkin_daily_state (
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    local_date TEXT NOT NULL,
    timezone TEXT NOT NULL,
    terminal_outcome TEXT,
    last_run_id TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider, account_id, local_date, timezone)
);

CREATE TABLE IF NOT EXISTS oauth_flows (
    state_hash TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    label TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    account_id TEXT
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    session_hash TEXT PRIMARY KEY,
    csrf_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    value_version INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'runtime',
    apply_mode TEXT NOT NULL DEFAULT 'immediate',
    apply_status TEXT NOT NULL DEFAULT 'applied',
    last_error TEXT,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS service_runtime (
    service_name TEXT PRIMARY KEY,
    desired_state TEXT NOT NULL,
    observed_state TEXT NOT NULL,
    worker_pid INTEGER,
    process_start_time REAL,
    process_group_id INTEGER,
    owner_instance_id TEXT,
    internal_auth_version INTEGER,
    started_at TEXT,
    stopped_at TEXT,
    last_health_at TEXT,
    last_exit_code INTEGER,
    last_error TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_operations (
    operation_id TEXT PRIMARY KEY,
    service_name TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS proxy_api_keys (
    key_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    scopes_json TEXT NOT NULL DEFAULT '["proxy"]',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    expires_at TEXT,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS model_catalog (
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'provider',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_seen_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (provider, model_id)
);

CREATE TABLE IF NOT EXISTS request_events (
    event_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    account_id TEXT,
    model_id TEXT NOT NULL,
    protocol TEXT NOT NULL,
    status TEXT NOT NULL,
    http_status INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    latency_ms INTEGER,
    stream_committed INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_code TEXT,
    redacted_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_request_events_started ON request_events(started_at);
CREATE INDEX IF NOT EXISTS idx_request_events_lookup ON request_events(provider, account_id, model_id);

CREATE TABLE IF NOT EXISTS usage_rollups (
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
    token_event_count INTEGER NOT NULL DEFAULT 0,
    missing_token_count INTEGER NOT NULL DEFAULT 0,
    latency_p50_ms INTEGER,
    latency_p95_ms INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (bucket_start, bucket_kind, provider, account_id, model_id)
);

CREATE TABLE IF NOT EXISTS account_metric_snapshots (
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    metric_kind TEXT NOT NULL,
    metric_value_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'fresh',
    last_error TEXT,
    PRIMARY KEY (provider, account_id, metric_kind)
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    result TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_events_created ON audit_events(created_at);

CREATE TABLE IF NOT EXISTS backup_runs (
    backup_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    size_bytes INTEGER,
    sha256 TEXT,
    error_message TEXT
);
"""

SCHEMA = BASE_SCHEMA + MANAGEMENT_SCHEMA_V4
