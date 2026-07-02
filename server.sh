#!/bin/bash
# qoderbuddy2api server management
set -e
cd "$(dirname "$0")"

# Activate venv
[ -d .venv ] && source .venv/bin/activate

# Load .env
if [ -f .env ]; then
    set -a; source .env; set +a
fi

PORT=${QB2API_PORT:-9999}

get_pid() {
    lsof -ti :"$PORT" 2>/dev/null
}

cmd_start() {
    get_pid > /dev/null && {
        echo "qoderbuddy2api already running on port $PORT (pid $(get_pid))"
        exit 1
    }
    echo "🚀 Starting qoderbuddy2api..."
    echo "   CodeBuddy: ${CODEBUDDY_TOKEN:+configured}"
    echo "   Qoder:     ${QODER_TOKEN:+configured}"
    echo "   Port:      $PORT"
    echo ""
    exec python -m qb2api
}

cmd_stop() {
    pid=$(get_pid)
    if [ -z "$pid" ]; then
        echo "qoderbuddy2api is not running on port $PORT."
        exit 0
    fi
    echo "Stopping qoderbuddy2api on port $PORT (pid $pid)..."
    kill "$pid" && echo "Stopped."
}

cmd_status() {
    pid=$(get_pid)
    if [ -n "$pid" ]; then
        echo "qoderbuddy2api is running on port $PORT (pid $pid)"
    else
        echo "qoderbuddy2api is not running on port $PORT."
    fi
}

case "${1:-}" in
    start)  cmd_start ;;
    stop)   cmd_stop ;;
    status) cmd_status ;;
    *)      echo "Usage: $0 {start|stop|status}" ; exit 1 ;;
esac
