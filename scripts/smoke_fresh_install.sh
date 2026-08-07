#!/usr/bin/env bash
# shellcheck source=smoke_common.sh
source "$(dirname "$0")/smoke_common.sh"

smoke_setup
trap cleanup_smoke EXIT
trap 'smoke_failure $?' ERR
control_port="$(free_port)"
worker_port="$(free_port)"
while [[ "$worker_port" == "$control_port" ]]; do worker_port="$(free_port)"; done

start_control "$control_port" "$worker_port"
wait_for_component "http://127.0.0.1:${CONTROL_PORT}/health" control-plane
wait_for_component "http://127.0.0.1:${WORKER_PORT}/internal/health/ready" proxy-worker true
assert_worker_models
crash_and_restart_worker
create_and_validate_backup
printf 'fresh install smoke passed\n'
