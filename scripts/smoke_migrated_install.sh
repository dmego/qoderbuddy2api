#!/usr/bin/env bash
# shellcheck source=smoke_common.sh
source "$(dirname "$0")/smoke_common.sh"

smoke_setup
trap cleanup_smoke EXIT
trap 'smoke_failure $?' ERR
"$PYTHON_BIN" - "$QB2API_DATA_DIR/qb2api.sqlite3" <<'PY'
import sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.executescript("""
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO schema_meta VALUES ('schema_version', '3');
CREATE TABLE accounts (provider TEXT NOT NULL, account_id TEXT NOT NULL, label TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'manual', enabled INTEGER NOT NULL DEFAULT 1, masked_identity TEXT, identity_hash TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (provider, account_id));
INSERT INTO accounts VALUES ('qoder', 'preserved', 'Preserved', 'manual', 1, NULL, NULL, '2026-07-23T00:00:00+00:00', '2026-07-23T00:00:00+00:00');
CREATE TABLE audit_events (event_id TEXT PRIMARY KEY, actor_type TEXT NOT NULL, actor_id TEXT, action TEXT NOT NULL, resource_type TEXT NOT NULL, resource_id TEXT, result TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
CREATE TABLE request_events (event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, provider TEXT NOT NULL, account_id TEXT, model_id TEXT NOT NULL, protocol TEXT NOT NULL, status TEXT NOT NULL, http_status INTEGER, input_tokens INTEGER, output_tokens INTEGER, latency_ms INTEGER, stream_committed INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL, finished_at TEXT, error_code TEXT, redacted_error TEXT);
""")
connection.commit(); connection.close()
PY
control_port="$(free_port)"
worker_port="$(free_port)"
while [[ "$worker_port" == "$control_port" ]]; do worker_port="$(free_port)"; done

start_control "$control_port" "$worker_port"
wait_for_component "http://127.0.0.1:${CONTROL_PORT}/health" control-plane
wait_for_component "http://127.0.0.1:${WORKER_PORT}/internal/health/ready" proxy-worker true
assert_worker_models
"$PYTHON_BIN" - "$QB2API_DATA_DIR/qb2api.sqlite3" <<'PY'
import sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
version = connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
account = connection.execute("SELECT label FROM accounts WHERE account_id='preserved'").fetchone()
connection.close()
if version != ('6',) or account != ('Preserved',): raise SystemExit('migration did not preserve expected state')
PY

stop_control
start_control "$control_port" "$worker_port"
wait_for_component "http://127.0.0.1:${CONTROL_PORT}/health" control-plane
wait_for_component "http://127.0.0.1:${WORKER_PORT}/internal/health/ready" proxy-worker true
assert_worker_models
printf 'migrated install smoke passed\n'
