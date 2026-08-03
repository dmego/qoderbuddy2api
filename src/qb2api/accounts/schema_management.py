"""Management-console schema additions introduced in schema version 4."""

MANAGEMENT_SCHEMA_V4 = """
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

CREATE TABLE IF NOT EXISTS metric_refresh_operations (
    operation_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
"""

MANAGEMENT_SCHEMA_V5 = """
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
"""

MANAGEMENT_SCHEMA = MANAGEMENT_SCHEMA_V4 + MANAGEMENT_SCHEMA_V5
