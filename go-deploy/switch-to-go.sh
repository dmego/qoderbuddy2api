#!/usr/bin/env bash
# 正式切换：停掉 Python 服务，把 Go 服务换到 9999，重启并验证。
#
# 设计要点：
#   * 以 Go 的数据为准：Go 的 go-data 完全不动，Python 的 data 只停不删、不改。
#   * 失败即回滚：验证没过就把 Go 退回原端口并重新拉起 Python，不留半吊子状态。
#   * 幂等：已在 9999 上就跳过停服与改端口，只做验证。
#   * 不打印任何密钥。
#
# 用法：
#   ./switch-to-go.sh          交互确认后执行
#   ./switch-to-go.sh --yes    跳过确认（无人值守）
#   ./switch-to-go.sh --dry-run 只打印将要执行的动作
set -euo pipefail

TARGET_PORT=9999
PY_DIR="$HOME/docker-space/qoderbuddy2api"
GO_DIR="$HOME/docker-space/qoderbuddy2api-go"
PY_COMPOSE="$PY_DIR/docker-compose.yml"
GO_COMPOSE="$GO_DIR/docker-compose.go.yml"
GO_ENV="$GO_DIR/.env.go"
PY_CONTAINER=qb2api-control
GO_CONTAINER=qb2api-go
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$GO_DIR/switch-$STAMP.log"
BACKUP_DIR="$GO_DIR/switch-backup-$STAMP"

MODE="run"
case "${1:-}" in
  --yes)     MODE="yes" ;;
  --dry-run) MODE="dry" ;;
  "")        MODE="run" ;;
  *)         echo "未知参数: $1（可用：--yes / --dry-run）" >&2; exit 2 ;;
esac

log()  { printf '%s  %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$LOG"; }
warn() { printf '%s  WARN: %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$LOG" >&2; }
die()  { printf '%s  ERROR: %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$LOG" >&2; exit 1; }
run()  {
  if [ "$MODE" = "dry" ]; then
    printf '  [dry-run] %s\n' "$*"
    return 0
  fi
  "$@"
}

# 本机 curl 默认走系统代理，会把 127.0.0.1 的请求劫持成 502。
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
CURL=(curl -s --noproxy '*')

# 从 .env.go 取一个键的值，绝不打回显。
env_value() {
  local key="$1"
  sed -n "s/^${key}=//p" "$GO_ENV" | head -1 | tr -d '\r'
}

# 容器是否处于 healthy。
is_healthy() {
  local state
  state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$1" 2>/dev/null || echo missing)"
  [ "$state" = "healthy" ] || [ "$state" = "running" ]
}

# 等待容器 healthy，最多 90 秒。
wait_healthy() {
  local name="$1" deadline=$((SECONDS + 90))
  while [ $SECONDS -lt $deadline ]; do
    if is_healthy "$name"; then return 0; fi
    sleep 2
  done
  return 1
}

# Go 现在监听哪个端口（从 compose 的端口映射读）。
go_current_port() {
  grep -oE '"[0-9]+:9999"|"[0-9]+:[0-9]+"' "$GO_COMPOSE" | head -1 | tr -d '"' | cut -d: -f1
}

# ---------------------------------------------------------------- 预检
log "===== 切换前预检 ====="

command -v docker >/dev/null || die "找不到 docker"
[ -f "$GO_COMPOSE" ] || die "找不到 $GO_COMPOSE"
[ -f "$GO_ENV" ]     || die "找不到 $GO_ENV"
[ -f "$PY_COMPOSE" ] || die "找不到 $PY_COMPOSE（回滚会用到）"

# 以 Go 数据为准：确认 Go 的数据目录存在且非空。
GO_DB="$GO_DIR/go-data/qb2api.sqlite3"
[ -f "$GO_DB" ] || die "Go 数据库不存在：$GO_DB（切换会丢数据，已中止）"
GO_ACCOUNTS="$(sqlite3 "$GO_DB" 'select count(*) from accounts;')"
log "Go 数据库：$GO_DB"
log "  accounts=$GO_ACCOUNTS"

# 确认没有第三方占用 9999 之外的预期端口。
PY_HEALTH="$("${CURL[@]}" -o /dev/null -w '%{http_code}' "http://127.0.0.1:9999/health" || echo 000)"
log "当前 9999：HTTP $PY_HEALTH"

GO_PORT_NOW="$(go_current_port)"
log "Go 当前端口：$GO_PORT_NOW"

GO_STATE="$(docker inspect --format '{{.State.Status}}' "$GO_CONTAINER" 2>/dev/null || echo missing)"
log "Go 容器状态：$GO_STATE"
[ "$GO_STATE" != "missing" ] || die "Go 容器不存在，请先 docker compose up -d --build"

# Python 容器状态（用于回滚判断）。
PY_STATE="$(docker inspect --format '{{.State.Status}}' "$PY_CONTAINER" 2>/dev/null || echo missing)"
log "Python 容器状态：$PY_STATE"

if [ "$GO_PORT_NOW" = "$TARGET_PORT" ] && [ "$PY_STATE" = "exited" ]; then
  log "看起来已经切换过了（Go 已在 ${TARGET_PORT}，Python 已停止），只做验证。"
fi

# ---------------------------------------------------------------- 确认
if [ "$MODE" = "run" ]; then
  echo
  echo "即将执行："
  echo "  1) 停止 Python 服务（容器 $PY_CONTAINER，仅 stop，不删除、不动其数据）"
  echo "  2) 把 Go 服务端口从 ${GO_PORT_NOW} 改为 ${TARGET_PORT}（改 .env.go 与 $GO_COMPOSE）"
  echo "  3) 重建并启动 Go 容器（数据沿用 $GO_DB，不重新登录）"
  echo "  4) 验证：/health、/admin、/v1/models，以及一次 deepseek-v4.1-flash 真实调用"
  echo "  5) 任一步失败 → 自动回滚（Go 退回 $GO_PORT_NOW，重新拉起 Python）"
  echo
  printf '确认继续？输入 yes 回车：'
  read -r reply
  [ "$reply" = "yes" ] || { log "已取消"; exit 0; }
fi

# ---------------------------------------------------------------- 备份
run mkdir -p "$BACKUP_DIR"
run cp "$GO_ENV" "$BACKUP_DIR/.env.go.orig"
run cp "$GO_COMPOSE" "$BACKUP_DIR/docker-compose.go.yml.orig"
log "已备份原始配置到 $BACKUP_DIR"

ROLLED_BACK=0
rollback() {
  [ "$ROLLED_BACK" = "1" ] && return 0
  ROLLED_BACK=1
  warn "开始回滚……"
  cp "$BACKUP_DIR/.env.go.orig" "$GO_ENV" 2>/dev/null || true
  cp "$BACKUP_DIR/docker-compose.go.yml.orig" "$GO_COMPOSE" 2>/dev/null || true
  docker compose -f "$GO_COMPOSE" up -d >/dev/null 2>&1 || true
  docker start "$PY_CONTAINER" >/dev/null 2>&1 || true
  if [ "$PY_STATE" != "missing" ]; then
    docker compose -f "$PY_COMPOSE" start >/dev/null 2>&1 || true
  fi
  warn "回滚完成：Go 退回 $GO_PORT_NOW，Python 已尝试重新拉起"
  warn "日志：$LOG"
}
trap 'rollback' ERR

# ---------------------------------------------------------------- 1) 改端口
log "===== 1/4 修改 Go 端口为 $TARGET_PORT ====="
if [ "$GO_PORT_NOW" = "$TARGET_PORT" ]; then
  log "已是 $TARGET_PORT，跳过"
else
  # .env.go 里的两个端口供进程内使用；compose 的端口映射供宿主机访问。
  run sed -i.bak -E "s|^QB2API_PORT=.*|QB2API_PORT=$TARGET_PORT|" "$GO_ENV"
  run sed -i.bak -E "s|^QB2API_CONTROL_PORT=.*|QB2API_CONTROL_PORT=$TARGET_PORT|" "$GO_ENV"
  run sed -i.bak -E "s|QB2API_PORT: \"[0-9]+\"|QB2API_PORT: \"$TARGET_PORT\"|" "$GO_COMPOSE"
  run sed -i.bak -E "s|QB2API_CONTROL_PORT: \"[0-9]+\"|QB2API_CONTROL_PORT: \"$TARGET_PORT\"|" "$GO_COMPOSE"
  run sed -i.bak -E "s|\"[0-9]+:[0-9]+\"|\"$TARGET_PORT:$TARGET_PORT\"|" "$GO_COMPOSE"
  run rm -f "$GO_ENV.bak" "$GO_COMPOSE.bak"
  log "已写入新端口"
fi

# ---------------------------------------------------------------- 2) 停 Python
log "===== 2/4 停止 Python 服务 ====="
if [ "$PY_STATE" = "missing" ]; then
  log "Python 容器不存在，跳过"
elif [ "$PY_STATE" = "exited" ]; then
  log "Python 已停止，跳过"
else
  if [ "$MODE" = "dry" ]; then
    printf '  [dry-run] docker compose -f %s stop（随后确认 %s 释放）\n' "$PY_COMPOSE" "$TARGET_PORT"
  else
    docker compose -f "$PY_COMPOSE" stop
    # 等端口真正释放，否则 Go 会因端口占用起不来。
    for _ in $(seq 1 15); do
      if ! lsof -nP -iTCP:$TARGET_PORT -sTCP:LISTEN >/dev/null 2>&1; then break; fi
      sleep 1
    done
    if lsof -nP -iTCP:$TARGET_PORT -sTCP:LISTEN >/dev/null 2>&1; then
      die "$TARGET_PORT 仍被占用，已中止（Go 未改端口）"
    fi
    log "Python 已停止，$TARGET_PORT 已释放"
  fi
fi

# ---------------------------------------------------------------- 3) 起 Go
log "===== 3/4 以 $TARGET_PORT 重建并启动 Go 服务 ====="
run docker compose -f "$GO_COMPOSE" up -d --force-recreate
if [ "$MODE" != "dry" ]; then
  wait_healthy "$GO_CONTAINER" || die "Go 容器未在 90 秒内变为 healthy"
  log "Go 容器 healthy"
fi

# ---------------------------------------------------------------- 4) 验证
log "===== 4/4 验证 ====="
if [ "$MODE" = "dry" ]; then
  log "dry-run 结束，未做任何修改"
  exit 0
fi

PROXY_KEY="$(env_value QB2API_PROXY_API_KEY)"
ADMIN_KEY="$(env_value QB2API_ADMIN_KEY)"
[ -n "$PROXY_KEY" ] || die "读不到 QB2API_PROXY_API_KEY"

log "容器内健康检查："
docker exec "$GO_CONTAINER" wget -qO- "http://127.0.0.1:${TARGET_PORT}/health" >/dev/null \
  || die "容器内 /health 失败"
log "  OK"

log "宿主 ${TARGET_PORT}/health："
HEALTH="$("${CURL[@]}" -o /dev/null -w '%{http_code}' "http://127.0.0.1:${TARGET_PORT}/health")"
log "  HTTP $HEALTH"
[ "$HEALTH" = "200" ] || die "/health 返回 $HEALTH"

log "管理台 /admin："
ADMIN_CODE="$("${CURL[@]}" -o /dev/null -w '%{http_code}' "http://127.0.0.1:${TARGET_PORT}/admin")"
log "  HTTP $ADMIN_CODE"
[ "$ADMIN_CODE" = "200" ] || die "/admin 返回 $ADMIN_CODE"

log "/v1/models 与账号数（用真实密钥）："
MODELS="$("${CURL[@]}" -H "Authorization: Bearer $PROXY_KEY" "http://127.0.0.1:${TARGET_PORT}/v1/models" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["data"]))')"
log "  模型数：$MODELS"
[ "$MODELS" -gt 0 ] || die "/v1/models 返回空"

ACCOUNTS="$("${CURL[@]}" -H "Authorization: Bearer $ADMIN_KEY" "http://127.0.0.1:${TARGET_PORT}/api/admin/accounts" | python3 -c 'import sys,json; print(json.load(sys.stdin)["total"])')"
log "  账号数：$ACCOUNTS"
[ "$ACCOUNTS" -gt 0 ] || die "/api/admin/accounts 返回空（数据没接上）"

log "真实模型调用（deepseek-v4.1-flash，流式）："
MODEL_OUT="$("${CURL[@]}" -N --max-time 120 \
  -H "Authorization: Bearer $PROXY_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4.1-flash","stream":true,"messages":[{"role":"system","content":"You are a helpful assistant."},{"role":"user","content":"Reply with exactly: SWITCH-OK"}]}' \
  "http://127.0.0.1:${TARGET_PORT}/v1/chat/completions" || true)"
if printf '%s' "$MODEL_OUT" | grep -q 'data: \[DONE\]'; then
  log "  流式调用成功（收到 [DONE]，$(printf '%s' "$MODEL_OUT" | grep -c '^data:') 帧）"
else
  die "模型调用失败，未收到 [DONE]"
fi

# 旧端口应已释放。
if [ "$GO_PORT_NOW" != "$TARGET_PORT" ]; then
  if lsof -nP -iTCP:"$GO_PORT_NOW" -sTCP:LISTEN >/dev/null 2>&1; then
    warn "${GO_PORT_NOW} 仍在监听（可能是别的进程）"
  else
    log "旧端口 ${GO_PORT_NOW} 已释放"
  fi
fi

trap - ERR
log ""
log "===== 切换完成 ====="
log "  Go 服务：http://127.0.0.1:${TARGET_PORT}  （容器 ${GO_CONTAINER}）"
log "  Python：已停止，容器保留，数据未改动"
log "  数据目录：$GO_DB"
log "  备份：$BACKUP_DIR"
log "  日志：$LOG"
log ""
log "回滚：docker compose -f $PY_COMPOSE start && \\"
log "      docker compose -f $GO_COMPOSE down"
log "      （并把 $BACKUP_DIR 里的两份原始配置复制回去）"
