# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`AGENTS.md` 是本仓库的流程规则（任务粒度、并行 wave、review 与 commit 策略）。
本文件只覆盖命令与架构，不重复流程规则。

## 命令

Python（在仓库根目录执行，以便加载 `.env`）：

```bash
.venv/bin/pytest -q                                   # 全量测试
.venv/bin/pytest tests/test_design_alignment.py -q     # 单文件
.venv/bin/pytest tests/test_design_alignment.py::test_proxy_and_admin_keys_must_differ
.venv/bin/ruff check src tests
.venv/bin/python -m compileall -q src/qb2api
.venv/bin/python tools/check_code_limits.py           # 体积/复杂度门禁，见下文"硬性约束"
.venv/bin/qb2api --mode control                       # 启动 Control Plane（它会拉起 Worker）
```

`pytest` 配置为 `asyncio_mode = "auto"`，异步测试无需加装饰器。

前端（`cd frontend`；安装依赖使用 `--registry=https://registry.npmmirror.com`）：

```bash
npm run test          # vitest，位于 tests/*.spec.ts
npm run test -- accounts.spec.ts
npm run typecheck     # vue-tsc
npm run lint
npm run build         # 改动 UI 后必须执行，原因见下文"Admin UI 构建"
npm run test:e2e      # playwright；会拉起 frontend/e2e/control_server.py 于 :19299
```

设置 `QB2API_E2E_REUSE_SERVER=1` 可复用已在运行的 e2e control server。

安装冒烟脚本（临时数据目录、真实双进程启动、Worker 崩溃重启、备份 dry-run）：

```bash
PYTHON_BIN=.venv/bin/python bash scripts/smoke_fresh_install.sh
PYTHON_BIN=.venv/bin/python bash scripts/smoke_migrated_install.sh
```

## 架构

### 双进程，单一所有权

**Control Plane**（`src/qb2api/control/`，默认 `:9999`）是唯一的常驻服务，拥有 Admin UI、
SQLite、凭据 vault、各类 scheduler、备份，以及 Worker 的生命周期。
**Proxy Worker**（`src/qb2api/worker/`，默认 `127.0.0.1:10001`）只负责 `/v1`（OpenAI）与
`/v1/messages`（Anthropic）的模型流量，不承担其他职责。

`ServiceSupervisor`（`control/supervisor.py`）以
`python -m uvicorn qb2api.worker.app:app` 方式拉起 Worker 子进程。
`control/worker_process.py` 会刻意把 `QB2API_ADMIN_KEY`、`QB2API_CREDENTIAL_KEY`、
`QB2API_PROXY_API_KEY`、`CODEBUDDY_TOKEN`、`QODER_TOKEN` 在子进程环境中置空——Worker 永远
不打开 SQLite、不解密凭据、也拿不到 Admin Key。不要再创建第二个 Worker 服务单元。

不要造"单进程同时跑两边"的捷径：`--mode combined` 是已废弃别名，会回落到 `control`；
`qb2api/app.py` 也只是重新导出 Control 应用。

### Control → Worker：runtime snapshot

Worker 的全部输入来自一份不可变的 `RuntimeSnapshot`（`src/qb2api/runtime_snapshot.py`，
`RUNTIME_PROTOCOL_VERSION = 2`）。Worker 通过 loopback 携带 `X-QB2API-Worker-Token` POST
`/api/control/worker/handshake`；Control Plane 会校验 loopback、token、协议版本，以及
`owner_instance_id` / `internal_auth_version` 与 supervisor 记录的进程身份是否一致，通过后才返回
snapshot。遥测数据沿同一通道回流到 `/api/control/telemetry`。

snapshot 中承载 provider token 明文，但 proxy key 只放 **sha256 摘要**。扩展 snapshot 时必须
同时更新 `to_payload()` 与严格校验的 `from_payload()`；任何不兼容改动都要提升
`RUNTIME_PROTOCOL_VERSION`——版本不匹配是硬性 409。

配置变更链路：mutation → `app.state.refresh_provider_pools` → registry rebuild → snapshot 版本
递增 → `supervisor.reload()` → Worker `/internal/runtime/reload` → 重新握手。
`WorkerRuntime.apply()` 会复用签名（`v{credential_version}:{sha256}`）未变的 provider，因此
reload 不会打断健康的上游连接。

### 进程身份安全

supervisor 在 spawn 时记录 `(pid, start_time, pgid, owner_instance_id, internal_auth_version)`，
五项不全部匹配就拒绝发送 SIGTERM / SIGKILL（`_terminate_verified` / `_kill_verified` 直接抛错）。
stop 会先按 `PROVIDER_DRAIN_TIMEOUT_SECONDS` 等待 `in_flight` 排空再发信号。所有操作由单个锁串行化，
并按 `idempotency_key` 去重。改动生命周期代码时必须保留这些不变量。

### Provider 池与流式 commit 规则

`DynamicProviderPool`（`providers/lb.py`）按稳定的 slot key 做 round-robin，失败 slot 进入 30s 冷却，
retiring slot 会等 `in_flight == 0` 后再关闭。

**failover 只允许在 commit 之前**：一旦第一个非空 chunk 已经下发给下游，`_PrecommitStreamFailure`
不再适用，错误直接向上抛。已经开始输出后，绝不能加跨账号重试。

### 账号、purpose 与凭据

`AccountRegistry`（`accounts/registry.py`）合并两个来源：静态 env token（只存在于进程内存，
**永不写入 SQLite**）与持久化的 DB 账号。把 env 账号 promote 之后，原 env slot 被标记为 `shadowed`。
slot 按 purpose 划分（`chat`、`checkin`）：`snapshot("chat")` 要求 `status == active`，
`snapshot("checkin")` 还额外要求 `verification_status == verified`。

`CredentialResolver`（`accounts/resolver.py`）以 `(provider, account_id, purpose)` 为键做缓存，
每键一把 single-flight 锁，在 `expires_at` 前的 skew 窗口内触发刷新；`checkin` 凭据缺失时会回落到
`chat` 凭据，模式记为 `inherit_chat`。

`AccountRepository`（`accounts/repository.py`）是基于单个 `aiosqlite` 连接的 mixin 组合
（`repo_*.py`）。所有操作都经过 `_operation_lock`；`transaction()` 使用 `BEGIN IMMEDIATE` 且
不可重入。迁移是增量式的（`_ensure_column`），基线 schema 在 `accounts/schema.py`。凭据写入通过
`expected_version` 支持 CAS（`CredentialVersionConflict`）。

`RuntimeServices`（`src/qb2api/runtime.py`）负责构造并且只关闭一次上述全部资源。它在启动时会撤销
所有 admin session——所以重启 Control Plane 会导致浏览器登出，这是设计行为，不是 bug。

### 鉴权

`admin/auth.py::classify_path` 是 method + path → 鉴权类别的唯一事实源。Control 中间件
（`control/request_auth.py`）与 Worker 边界中间件（`worker/app.py`）都消费它；要改矩阵就改这里，
不要在各个 handler 里改。

三个 key 分属不同信任域，由 `Settings.validate_startup()` 强制：`QB2API_PROXY_API_KEY`
（模型客户端 → Worker）、`QB2API_ADMIN_KEY`（管理）、`QB2API_CREDENTIAL_KEY`（Fernet，凭据加密）。
Proxy Key 与 Admin Key 必须不同；启用 admin UI / check-in / 持久化凭据时，Admin Key 与
Credential Key 都是必填。

Worker 边界会对 `/admin`、`/api/admin`、`/api/config`、`/static/admin` 直接返回 404。Admin session
是 HttpOnly cookie，非 GET 请求额外要求 `X-CSRF-Token` 头（`frontend/src/api/client.ts`）；
`resolve_cookie_secure` 允许 loopback HTTP，仅在显式设置 `QB2API_ADMIN_COOKIE_SECURE=false` 时
允许受信 LAN / Tailscale HTTP，其余场景一律要求 HTTPS。

### Admin UI 构建

`frontend/` 中的 Vue 3 SPA 以 base `/admin/` 构建输出到 `src/qb2api/web/dist`
（`frontend/vite.config.ts`）；Control Plane 将 `dist/index.html` 作为 SPA fallback 提供，并挂载
`dist/assets`。**前端源码改动在执行 `npm run build` 之前完全不可见**——`dist` 是被提交进仓库的构建
产物。`dist` 出现合并冲突时，取源码侧并重新构建，不要手工合并带 hash 的文件。

## 硬性约束

`tools/check_code_limits.py` 对 `src/qb2api`、`frontend/src`、`tests`（tests 只检查文件行数）强制：
单文件 500 行、单函数 50 行、圈复杂度 10、嵌套深度 3、位置参数 3 个。

后 4 条作用于单个函数内部，压低它们没有反向代价，应当遵守。文件行数上限的作用只是拦住失控的
上帝文件——**不要为了贴合它而把一个内聚的概念切成 `*_support` / `*_helpers` / `*_filters` 卫星
文件**。历史上 300 行的上限制造过一批这类文件（已合并回主文件），拆分点应该由领域边界决定，
不由行数决定。

原始 token、cookie、API key、`Authorization` 值、上游响应正文以及 prompt / completion 内容，都不得
进入日志、审计记录、SQLite、浏览器存储或提交记录。遥测入库对字段做了 allowlist，请保持这一做法。

通过 API 的备份恢复只做校验（checksum + 完整性 + schema），并返回 `offline_restore_required`；
真正的恢复必须先停掉 Control Plane 再进行。
