#!/usr/bin/env bash
# Start the Go service for Playwright acceptance tests.
#
# Runs on a private port with a throwaway data directory and freshly generated
# keys, so a test run can never read or write a developer's real .env or
# database. Credentials are generated here rather than sourced from the
# environment for the same reason.
set -euo pipefail

ROOT="${QB2API_E2E_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
PORT="${QB2API_E2E_CONTROL_PORT:-19299}"
DATA_DIR="$(mktemp -d "${TMPDIR:-/tmp}/qb2api-e2e-XXXXXX")"

cleanup() { rm -rf "$DATA_DIR"; }
trap cleanup EXIT

# A Fernet key is 32 url-safe base64 bytes; generate one without Python.
CREDENTIAL_KEY="$(head -c 32 /dev/urandom | base64 | tr '+/' '-_')"

export QB2API_HOST=127.0.0.1
export QB2API_PORT="$PORT"
export QB2API_DATA_DIR="$DATA_DIR"
export QB2API_LOG_DIR="$DATA_DIR/logs"
export QB2API_MODEL_CONFIG="$ROOT/config/models.json"
export QB2API_WEB_DIR="$ROOT/web/dist"
export QB2API_PROXY_API_KEY=playwright-proxy-key
export QB2API_ADMIN_KEY=playwright-admin-key
export QB2API_CREDENTIAL_KEY="$CREDENTIAL_KEY"
export QB2API_ADMIN_UI_ENABLED=true
# Tests drive the console over plain loopback HTTP.
export QB2API_ADMIN_COOKIE_SECURE=false
# Keep the schedulers out of the way: they would reach for real upstreams.
export CHECKIN_ENABLED=false
export QB2API_METRICS_ENABLED=false
export GROWTH_SCHEDULER_ENABLED=false
export QB2API_CREDENTIAL_REFRESH_ENABLED=false
export QB2API_LOG_LEVEL=warn

exec go run "$ROOT/cmd/qb2api"
