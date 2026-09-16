// Package store owns the SQLite schema, migrations and repositories.
//
// The DDL below is byte-compatible with the schema the Python control plane
// left behind, so the Go rewrite can be pointed at the existing database file
// without a migration step. New columns are additive and applied by migrate().
package store

// baseSchema mirrors src/qb2api/accounts/schema.py BASE_SCHEMA plus
// MANAGEMENT_SCHEMA. Every statement is IF NOT EXISTS so migrate() is
// idempotent against a populated database.
const baseSchema = `
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
    reward_credits REAL,
    reward_expires_at TEXT,
    quota_before_json TEXT,
    quota_after_json TEXT,
    quota_delta_json TEXT,
    quota_observed_at TEXT,
    quota_change_status TEXT,
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

CREATE TABLE IF NOT EXISTS workbuddy_active_days (
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    local_date TEXT NOT NULL,
    timezone TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    error_code TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    updated_at TEXT NOT NULL,
    confirmed TEXT,
    confirmed_at TEXT,
    confirm_attempts INTEGER NOT NULL DEFAULT 0,
    run_attempts INTEGER NOT NULL DEFAULT 0,
    official_score INTEGER,
    official_streak_days INTEGER,
    official_updated_at TEXT,
    official_observed_at TEXT,
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

CREATE TABLE IF NOT EXISTS service_events (
    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    service_name TEXT NOT NULL,
    event_type TEXT NOT NULL,
    action TEXT,
    desired_state TEXT,
    observed_state TEXT,
    operation_id TEXT,
    status TEXT,
    in_flight INTEGER,
    error_code TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_service_events_cursor ON service_events(cursor DESC);

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
    redacted_error TEXT,
    reasoning_effort TEXT,
    first_token_ms INTEGER
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
    ttft_avg_ms INTEGER,
    ttft_p95_ms INTEGER,
    latency_avg_ms INTEGER,
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

CREATE TABLE IF NOT EXISTS account_metric_history (
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    metric_kind TEXT NOT NULL,
    metric_value_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'fresh',
    PRIMARY KEY (provider, account_id, metric_kind, observed_at)
);

CREATE INDEX IF NOT EXISTS idx_account_metric_history_lookup
ON account_metric_history(provider, account_id, metric_kind, observed_at DESC);

CREATE TABLE IF NOT EXISTS metric_refresh_operations (
    operation_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
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

CREATE TABLE IF NOT EXISTS growth_automation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    triggered_by TEXT NOT NULL,
    results_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_growth_log_account
    ON growth_automation_log(provider, account_id, created_at DESC);

CREATE TABLE IF NOT EXISTS model_route_policies (
    model_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    weight INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (model_id, provider)
);

CREATE TABLE IF NOT EXISTS account_model_blocks (
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    blocked_until TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider, account_id, model_id)
);
`

// migrations are applied after baseSchema: each adds a column that a previous
// generation of the schema may not have. Table+column+definition triples.
var migrations = []struct{ Table, Column, Definition string }{
	{"usage_rollups", "token_event_count", "INTEGER NOT NULL DEFAULT 0"},
	{"usage_rollups", "missing_token_count", "INTEGER NOT NULL DEFAULT 0"},
	{"usage_rollups", "ttft_avg_ms", "INTEGER"},
	{"usage_rollups", "ttft_p95_ms", "INTEGER"},
	{"usage_rollups", "latency_avg_ms", "INTEGER"},
	{"service_events", "in_flight", "INTEGER"},
	{"request_events", "reasoning_effort", "TEXT"},
	{"request_events", "first_token_ms", "INTEGER"},
	{"checkin_attempts", "reward_credits", "REAL"},
	{"checkin_attempts", "reward_expires_at", "TEXT"},
	{"checkin_attempts", "quota_before_json", "TEXT"},
	{"checkin_attempts", "quota_after_json", "TEXT"},
	{"checkin_attempts", "quota_delta_json", "TEXT"},
	{"checkin_attempts", "quota_observed_at", "TEXT"},
	{"checkin_attempts", "quota_change_status", "TEXT"},
	{"workbuddy_active_days", "confirmed", "TEXT"},
	{"workbuddy_active_days", "confirmed_at", "TEXT"},
	{"workbuddy_active_days", "confirm_attempts", "INTEGER NOT NULL DEFAULT 0"},
	{"workbuddy_active_days", "run_attempts", "INTEGER NOT NULL DEFAULT 0"},
	{"workbuddy_active_days", "official_score", "INTEGER"},
	{"workbuddy_active_days", "official_streak_days", "INTEGER"},
	{"workbuddy_active_days", "official_updated_at", "TEXT"},
	{"workbuddy_active_days", "official_observed_at", "TEXT"},
}

// schemaVersion is bumped when a migration above lands.
const schemaVersion = "8"

// refreshableProviders mirrors accounts/refresh.py REFRESHABLE_PROVIDERS for the
// providers the rewrite keeps.
var refreshableProviders = []string{"workbuddy_intl"}
