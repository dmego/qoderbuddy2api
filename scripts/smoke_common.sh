#!/usr/bin/env bash
# Shared helpers for the installation smoke scripts.
set -euo pipefail

SMOKE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${PYTHON_BIN:=python3}"
: "${QB2API_SMOKE_TIMEOUT_SECONDS:=20}"
SMOKE_DIR=""
CONTROL_PID=""
CONTROL_PORT=""
WORKER_PORT=""

smoke_setup() {
  umask 077
  SMOKE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/qb2api-smoke.XXXXXX")"
  mkdir -p "$SMOKE_DIR/data" "$SMOKE_DIR/logs"
  export QB2API_DATA_DIR="$SMOKE_DIR/data" QB2API_LOG_DIR="$SMOKE_DIR/logs"
  export QB2API_ADMIN_KEY="admin-smoke-${RANDOM}-${RANDOM}"
  export QB2API_PROXY_API_KEY="proxy-smoke-${RANDOM}-${RANDOM}"
  export QB2API_CREDENTIAL_KEY
  QB2API_CREDENTIAL_KEY="$("$PYTHON_BIN" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
  export QB2API_WORKER_INTERNAL_TOKEN="internal-smoke-${RANDOM}-${RANDOM}"
  export QB2API_WORKER_AUTOSTART=true QB2API_WORKER_START_TIMEOUT_SECONDS=8
  export QB2API_ADMIN_UI_ENABLED=true QB2API_LOG_REQUESTS=false
  export CODEBUDDY_TOKEN=ck-smoke QODER_TOKEN=
}

free_port() {
  "$PYTHON_BIN" - <<'PY'
import socket
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    print(listener.getsockname()[1])
PY
}

start_control() {
  CONTROL_PORT="$1"; WORKER_PORT="$2"
  export QB2API_CONTROL_HOST=127.0.0.1 QB2API_CONTROL_PORT="$CONTROL_PORT"
  export QB2API_WORKER_HOST=127.0.0.1 QB2API_WORKER_PORT="$WORKER_PORT"
  "$PYTHON_BIN" -m qb2api --mode control --host 127.0.0.1 --port "$CONTROL_PORT" \
    >>"$SMOKE_DIR/control.log" 2>&1 &
  CONTROL_PID=$!
}

stop_control() {
  [[ -n "$CONTROL_PID" ]] || return
  kill -TERM "$CONTROL_PID" 2>/dev/null || true
  for _ in $(seq 1 100); do
    if ! kill -0 "$CONTROL_PID" 2>/dev/null; then
      wait "$CONTROL_PID" 2>/dev/null || true; CONTROL_PID=""; return
    fi
    sleep 0.1
  done
  kill -KILL "$CONTROL_PID" 2>/dev/null || true
  wait "$CONTROL_PID" 2>/dev/null || true
  CONTROL_PID=""
}

cleanup_smoke() {
  stop_control
  if [[ "${QB2API_SMOKE_KEEP:-0}" != "1" && -n "$SMOKE_DIR" ]]; then rm -rf -- "$SMOKE_DIR"; fi
  if [[ "${QB2API_SMOKE_KEEP:-0}" == "1" && -n "$SMOKE_DIR" ]]; then printf 'Smoke artifacts retained at %s\n' "$SMOKE_DIR" >&2; fi
}

smoke_failure() {
  local status="$1"
  printf 'qoderbuddy2api smoke failed; recent Control Plane log follows:\n' >&2
  tail -n 100 "$SMOKE_DIR/control.log" >&2 || true
  exit "$status"
}

wait_for_component() {
  local url="$1" component="$2" internal="${3:-false}"
  SMOKE_URL="$url" SMOKE_COMPONENT="$component" SMOKE_INTERNAL="$internal" \
    SMOKE_TOKEN="$QB2API_WORKER_INTERNAL_TOKEN" SMOKE_TIMEOUT="$QB2API_SMOKE_TIMEOUT_SECONDS" \
    "$PYTHON_BIN" - <<'PY'
import json, os, time, urllib.error, urllib.request
deadline = time.monotonic() + float(os.environ["SMOKE_TIMEOUT"])
headers = {"X-QB2API-Worker-Token": os.environ["SMOKE_TOKEN"]} if os.environ["SMOKE_INTERNAL"] == "true" else {}
last_error = "not attempted"
while time.monotonic() < deadline:
    try:
        request = urllib.request.Request(os.environ["SMOKE_URL"], headers=headers)
        with urllib.request.urlopen(request, timeout=1) as response: payload = json.load(response)
        if response.status == 200 and payload.get("component") == os.environ["SMOKE_COMPONENT"]: raise SystemExit(0)
        last_error = f"unexpected payload: {payload!r}"
    except (OSError, ValueError, urllib.error.HTTPError) as error: last_error = type(error).__name__
    time.sleep(0.1)
raise SystemExit(f"endpoint did not become ready: {last_error}")
PY
}

admin_json() {
  local method="$1" url="$2" body="${3:-}"
  if [[ -z "$body" ]]; then body='{}'; fi
  SMOKE_METHOD="$method" SMOKE_URL="$url" SMOKE_BODY="$body" SMOKE_ADMIN_KEY="$QB2API_ADMIN_KEY" \
    "$PYTHON_BIN" - <<'PY'
import json, os, urllib.error, urllib.request
request = urllib.request.Request(os.environ["SMOKE_URL"], data=os.environ["SMOKE_BODY"].encode(), method=os.environ["SMOKE_METHOD"].upper(), headers={"Authorization": f"Bearer {os.environ['SMOKE_ADMIN_KEY']}", "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status not in {200, 201, 202}: raise RuntimeError("unexpected status")
        print(json.dumps(json.load(response)))
except (OSError, ValueError, urllib.error.HTTPError) as error:
    raise SystemExit(f"admin request failed: {type(error).__name__}") from error
PY
}

assert_worker_models() {
  SMOKE_URL="http://127.0.0.1:${WORKER_PORT}/v1/models" SMOKE_PROXY_KEY="$QB2API_PROXY_API_KEY" "$PYTHON_BIN" - <<'PY'
import json, os, urllib.request
request = urllib.request.Request(os.environ["SMOKE_URL"], headers={"Authorization": f"Bearer {os.environ['SMOKE_PROXY_KEY']}"})
with urllib.request.urlopen(request, timeout=5) as response: payload = json.load(response)
if response.status != 200 or not payload.get("data"): raise SystemExit("Worker model discovery was unavailable")
PY
}

crash_and_restart_worker() {
  local pid
  pid="$(admin_json GET "http://127.0.0.1:${CONTROL_PORT}/api/admin/service" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["identity"]["pid"])')"
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  kill -TERM "$pid"; sleep 0.2
  admin_json POST "http://127.0.0.1:${CONTROL_PORT}/api/admin/service/restart" '{}' >/dev/null
  wait_for_component "http://127.0.0.1:${WORKER_PORT}/internal/health/ready" proxy-worker true
  assert_worker_models
}

create_and_validate_backup() {
  local backup_id
  backup_id="$(admin_json POST "http://127.0.0.1:${CONTROL_PORT}/api/admin/backup" '{}' | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["backup_id"])')"
  admin_json POST "http://127.0.0.1:${CONTROL_PORT}/api/admin/backup/${backup_id}/restore" '{"dry_run":true}' | "$PYTHON_BIN" -c '
import json, sys
payload = json.load(sys.stdin)
if payload.get("dry_run") is not True or payload.get("next_step") != "offline_restore_required": raise SystemExit("backup dry-run failed")
'
}
