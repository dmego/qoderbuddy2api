#!/usr/bin/env bash
# Unified model catalog smoke: canonical lowercase ids, legacy compat, routing.
set -euo pipefail

SMOKE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHON_BIN="${PYTHON_BIN:-$SMOKE_ROOT/.venv/bin/python}"
# shellcheck source=scripts/smoke_common.sh
source "$SMOKE_ROOT/scripts/smoke_common.sh"
trap cleanup_smoke EXIT

smoke_setup
CONTROL_PORT="$(free_port)"
WORKER_PORT="$(free_port)"
start_control "$CONTROL_PORT" "$WORKER_PORT"
wait_for_component "http://127.0.0.1:${CONTROL_PORT}/health" control-plane
wait_for_component "http://127.0.0.1:${WORKER_PORT}/internal/health/ready" proxy-worker true
assert_worker_models

SMOKE_URL="http://127.0.0.1:${WORKER_PORT}" SMOKE_PROXY_KEY="$QB2API_PROXY_API_KEY" "$PYTHON_BIN" - <<'PY'
import json, os, urllib.error, urllib.request

BASE = os.environ["SMOKE_URL"]
HEADERS = {"Authorization": f"Bearer {os.environ['SMOKE_PROXY_KEY']}", "Content-Type": "application/json"}

def get(path):
    request = urllib.request.Request(BASE + path, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.load(response)

def post(path, body):
    request = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)

status, payload = get("/v1/models")
ids = [item["id"] for item in payload["data"]]
assert status == 200 and ids == sorted(ids), (status, ids)
for model_id in ids:
    assert "/" not in model_id, model_id
    assert model_id == model_id.lower(), model_id
print(f"unified models ({len(ids)}): {ids}")

status, props = get("/v1/props")
assert status == 200 and isinstance(props["models"], list), (status, props)
print(f"v1/props models: {props['models']}")

body = {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "ping"}]}
status, result = post("/v1/chat/completions", body)
message = json.dumps(result, ensure_ascii=False)
assert "Unknown model" not in message, message
assert status in {200, 401, 502}, (status, message)
print(f"deepseek-v4-flash routed (upstream status {status}), not a model-resolution error")

status, result = post("/v1/chat/completions", {**body, "model": "codebuddy/deepseek-v4-flash"})
message = json.dumps(result, ensure_ascii=False)
assert "Unknown model" not in message, message
assert status in {200, 401, 502}, (status, message)
print(f"legacy prefixed id still resolves (upstream status {status})")

status, result = post("/v1/chat/completions", {**body, "model": "totally-unknown-model"})
assert status == 400 and "Unknown model" in json.dumps(result), (status, result)
print("unknown model rejected with 400")
PY
