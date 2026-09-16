# 系统架构设计

> 本文档描述 qoderbuddy2api 的系统架构与安全模型，是代码实现的事实依据。
> 随实现演进持续更新；协议级外部契约以真实 Spike 证据与脱敏记录规则为准。

## 1. 概述

qoderbuddy2api 是一个运行在自托管主机（本机 / 私有服务器）的**多账号模型网关与运维控制台**：
把 WorkBuddy（国内 `codebuddy`）与 WorkBuddy 国际版（`workbuddy_intl`）的多个账号
收敛为一个 OpenAI / Anthropic 兼容入口，并提供账号、凭据、签到、成长中心自动化、
用量与积分的统一管理面。

**单进程、单二进制**（Go）：

```text
Browser (admin) ──┐
CLI clients (/v1) ┴──> qb2api :9999
                        ├─ 代理：OpenAI / Anthropic 兼容路由
                        ├─ 管理 API / 管理台 SPA
                        ├─ SQLite（账号、凭据密文、遥测、调度状态）
                        └─ 调度器（签到 / 成长 / 指标 / 用量聚合 / 凭据轮换）
                              └─ 上游：copilot.tencent.com / www.workbuddy.ai
```

早期 Python 版本是 Control Plane + 受管 Proxy Worker 双进程，通过 loopback 上的
版本化 JSON 快照握手；Go 版把两个面合成一个进程，省掉了握手、快照序列化和第二份解释器
（实测常驻内存 322 MB → 25 MB）。

安全边界不靠进程隔离，而靠**类型**：代理处理路径只拿到 `*ProxyPlane`，它只暴露
provider 池与模型路由，拿不到数据库、Admin Key 或凭据主密钥。凭据只在每次池重建时
解密一次，以不透明的 bearer 串交给 provider。

## 2. 核心设计决策

| 决策 | 结论 |
| --- | --- |
| 进程形态 | Go 单进程、单静态二进制，代理与管理面同进程 |
| 对外入口 | 统一单端口 `9999`：`/admin`、`/api/admin/*`、`/v1/*` 均由同一进程处理 |
| 凭据分离 | `QB2API_PROXY_API_KEY`（代理）、`QB2API_ADMIN_KEY`（管理）、`QB2API_CREDENTIAL_KEY`（静态加密）三值互不相同 |
| 代理边界 | 代理处理路径只持有 `*ProxyPlane`（provider 池 + 路由），不打开 SQLite、不经手管理密钥 |
| 持久化 | SQLite 单写者；凭据字段用 `cryptography` Fernet 加密，版本化 CAS 写入 |
| 模型路由 | 统一模型目录 + 账号级轮询池，首个下游 chunk 前故障转移，输出后绝不跨账号重试 |
| 调度 | 进程内多调度器（签到 / 成长 / 指标 / 模型同步 / 用量聚合），账号间失败隔离 |
| 管理面 | 同源 Vue SPA + 管理 API；原始凭据只在后端与加密存储中出现 |
| 传输安全 | 默认 loopback HTTP / 远程 HTTPS；可信私网可显式降级，公网暴露不受支持 |
| 可观测 | 请求遥测、用量聚合、指标快照、审计事件、备份与恢复校验 |

## 3. 组件与职责

| 组件 | 职责 | 不负责 |
| --- | --- | --- |
| `internal/server` | HTTP 面、管理端鉴权与 CSRF、调度器装配 | 直接实现模型协议细节 |
| `internal/providers` | 上游客户端、账号池、跨 provider 路由、冷却与配额封锁 | 访问 SQLite、签发凭据 |
| `internal/store` | SQLite schema/迁移、各表仓储、遥测批写 | 解密凭据、发起上游请求 |
| `internal/vault` | Fernet 兼容的凭据加解密与指纹 | 向 UI 返回明文 |
| `AccountRegistry` | 账号元数据、purpose 状态、能力摘要、动态快照 | 直接发上游 HTTP |
| `CredentialVault` | 加密保存/读取 Secret、版本化、原子更新 | 向 UI 返回 Secret |
| `CredentialResolver` | 按 provider/account/purpose 解析临时凭据、单飞 refresh | 返回凭据给浏览器 |
| `DynamicProviderPool` | 账号选择、冷却、0..N 热替换、首块前 failover | 签到、管理 API、首块后的重放 |
| `CheckinService` / `GrowthAutomation` | 签到与成长中心自动化执行、分类、落库、账号隔离 | 计算调度时间 |
| 调度器家族 | 时区窗口、批次锁、生命周期、补跑 | 构造上游 HTTP 请求 |

### 3.1 进程模型

单进程，无子进程监督。启动顺序：加载配置 → 打开 SQLite 并迁移 → 建凭据 vault →
构建 provider 池（解密一次凭据）→ 启动调度器 → 开始监听。

- 收到 `SIGINT`/`SIGTERM` 后：停止调度器 → 停止接收新请求（20 秒优雅期）→
  刷写遥测批 → `wal_checkpoint(TRUNCATE)`。
- 凭据只在池重建时解密。重建由管理端变更（导入/轮换/启停账号、改路由策略）触发。
- 调度器各自独立循环，单账号失败不影响其他账号，也不影响代理面。

### 3.2 单一监听端口

```text
/v1/*           代理（模型请求，Bearer Proxy Key）
/api/admin/*    管理 API（Bearer Admin Key / 会话 Cookie + CSRF）
/admin          管理台 SPA（同源静态资源）
/health /version 公开探针
```

模型请求只携带 Proxy Key；管理面与代理面是两把独立密钥，互不通用。

## 4. 安全模型

### 4.1 信任域

| 密钥 | 使用方 | 泄露影响 |
| --- | --- | --- |
| `QB2API_PROXY_API_KEY` | 模型客户端 | 可消耗模型额度 |
| `QB2API_ADMIN_KEY` | 管理登录 | 可管理全部账号 |
| `QB2API_CREDENTIAL_KEY` | 凭据静态加密 | 可解密全部持久凭据 |

丢失 `QB2API_CREDENTIAL_KEY` 无法解密已存凭据；轮换它不会迁移旧数据。

### 4.2 传输与 Cookie

`QB2API_ADMIN_COOKIE_SECURE` 三态：

| 值 | 语义 |
| --- | --- |
| `auto`（默认） | 本机 HTTP 允许，远程 HTTP 拒绝 |
| `false` | 显式受信 Tailscale/LAN HTTP（不接受公网暴露） |
| `true` | 一律要求 HTTPS |

会话 Cookie 为 `HttpOnly` + `SameSite=Lax`，管理 API 附加 CSRF token。
转发头信任（`QB2API_TRUSTED_PROXY_HEADERS`）只在明确 HTTPS 反向代理直连对端 CIDR 时开启。

### 4.3 凭据加密与文件权限

- 持久凭据以 Fernet 加密写入 SQLite，加密密钥来自 `QB2API_CREDENTIAL_KEY`。
- 数据目录、日志目录收紧为 `0700`，SQLite 与备份文件为 `0600`。
- 原始 token、Cookie、Authorization、prompt/completion 不得写入日志、审计、SQLite、
  前端持久化或提交记录。

### 4.4 日志与审计边界

请求日志只记录脱敏元数据（状态码、模型、延迟、token 数）；审计事件只记录操作元数据；
上游响应正文、凭据与完整会话内容永不落盘。

## 5. 数据模型

SQLite（`data/qb2api.sqlite3`，schema 版本 7）主要表：

| 表 | 内容 |
| --- | --- |
| `accounts` / `account_purposes` | 账号与用途（chat / checkin），状态与能力 |
| `credentials` | 加密凭据，版本化 CAS |
| `checkin_runs` / `checkin_attempts` / `checkin_daily_state` | 签到批次、尝试与当日终态 |
| `workbuddy_active_days` | 登录自动化（活跃日）每日幂等记录 + 上游确认状态 |
| `growth_automation_log` | 成长中心自动化执行历史 |
| `model_catalog` | 模型目录（qoder 上游同步源） |
| `runtime_settings` | 版本化运行设置（管理台可持久化） |
| `proxy_api_keys` / `admin_sessions` / `oauth_flows` | 代理密钥、管理会话、OAuth 流程 |
| `request_events` / `usage_rollups` | 请求遥测与用量聚合 |
| `account_metric_snapshots` / `account_metric_history` | 配额/积分快照与历史 |
| `service_events` / `service_operations` | 服务生命周期事件与操作 |
| `audit_events` / `backup_runs` | 审计与备份记录 |

### 5.1 账号模型

账号以 `(provider, account_id)` 唯一标识；每个账号按用途（`chat`、`checkin`）维护独立
凭据、状态与能力。国内 `codebuddy` 的 chat 与签到复用同一账号 ID，但凭据与状态域
相互独立；国际版 `workbuddy_intl` 只有 chat（无签到与成长中心）。

### 5.2 凭据版本化

凭据写入使用 compare-and-swap：`credential_version` 冲突时拒绝覆盖，避免并发轮换
丢失更新。轮换、撤销、重派生均为受审计的管理操作。

## 6. 代理与模型路由

### 6.1 统一模型目录

- 对外只暴露规范小写 ID（如 `deepseek-v4.1-flash`、`glm-5.2`），不带 `provider/`
  前缀；两个 provider 共有的模型合并为单一条目，内部按路由策略轮询。
- 模型定义来自 `config/models.json`，`model_catalog` 表记录管理台的启停状态；
  定义文件的变更由管理台「刷新目录」写入并立即重建运行时。
- 旧前缀 ID（`codebuddy/glm-5.2`）与上游裸 ID 仍可解析（deprecated 兼容），
  但不再列出。

### 6.2 账号池与故障转移

每个提供商一个 `DynamicProviderPool`（账号级轮询、30s 冷却、0..N 热替换）。
请求经 `resolve_model` 得到 `(provider, upstream_model_id)`，上游 ID 原样保留。
**故障转移只允许在首个下游 chunk 之前**；流式输出开始后绝不跨账号重试。

### 6.3 兼容协议

OpenAI Chat Completions、Anthropic Messages 与 `/v1/models` 均在统一入口提供；
reasoning 内容默认剥离，可通过开关透传。

## 7. 自动化与调度

### 7.1 调度器家族

| 调度器 | 周期/时间 | 职责 |
| --- | --- | --- |
| CheckinScheduler | `checkin.at`（如 10:30）+ 时区 | 每日签到批次、catch-up 窗口、抖动 |
| GrowthScheduler | 每 30 分钟（最小 10 分钟） | 成长中心自动化：任务/抽奖/旅行/兑换/Buddy/登录 |
| MetricsScheduler | 每 15 分钟 | 配额/积分快照采集 |
| UsageRollup | 每 60 秒 | 请求明细聚合到趋势桶 |

调度器在同一进程内运行，按账号串行执行，单账号失败不阻断其他账号。
另有凭据轮换循环：按 `QB2API_CREDENTIAL_REFRESH_LEAD_SECONDS` 提前刷新即将到期的
短效 token（目前只有 `workbuddy_intl` 有刷新契约；国内凭据无刷新端点，到期前只告警）。

### 7.2 签到

每日批次按 `checkin_daily_state` 判定当日终态（CLAIMED / ALREADY_CHECKED_IN /
SKIPPED / FAILED），已终态账号后续运行直接跳过（幂等）。支持 manual / scheduler /
catch_up / verify 四种触发：manual 与 verify 不触发成长副作用。结果分类覆盖
`HTTP 400 + code=10001 → ALREADY_CHECKED_IN`、上游活动关闭（`qoder_checkin_disabled`）
等业务状态，网络类错误按 `checkin_retry_limit` 重试。

### 7.3 成长中心自动化

独立 GrowthScheduler 按固定间隔执行已启用步骤（`growth.auto_tasks/lottery/travel/
redeem/buddy_open`），每步返回结构化结果并写入 `growth_automation_log`；兑换档位
（7d/14d/28d）与开关可在管理台设置。

### 7.4 登录自动化（活跃日）

与签到完全解耦的独立步骤，保证成长中心连续登录天数累计：

- 调度：GrowthScheduler 启动即跑一轮、之后每间隔检查；`workbuddy_active_days` 以
  `(provider, account_id, local_date, timezone)` 唯一键 + `ON CONFLICT DO NOTHING`
  保证每账号每天最多一次真实 WorkBuddy ACP 对话。
- 前置检查：当天已被外部点亮时记 `skipped_external`，不消耗调用。
- 后置确认：每轮检查上游 `today.is_active`，点亮记 `confirmed=lit`；未点亮累加
  `confirm_attempts`，达到上限（默认 3）记 `not_lit`。
- 手动重跑：管理端 `POST .../growth/active-day/rerun` 绕过当日幂等锁强制重试
  （真实扣费，需显式操作）。

### 7.5 运行时设置

管理台「设置」保存到 `runtime_settings`（版本化、来源、应用模式）。应用模式分
`immediate`（热应用）、`scheduler_reschedule`（调度重排）、`restart_required`。
默认值来自 `.env`，运行时覆盖优先级更高。

## 8. 管理面

### 8.1 会话

`QB2API_ADMIN_KEY` 首次登录后建立 HttpOnly 会话；TTL/空闲超时由
`QB2API_ADMIN_SESSION_TTL_HOURS` / `IDLE_MINUTES` 控制。Control Plane 重启会撤销
全部会话。

### 8.2 管理 API 域

账号（导入/验证/轮换/删除）、模型（启停/探测/刷新）、用量与事件、指标历史、
签到批次与明细、服务生命周期、运行设置、代理密钥、审计、备份。所有变更写审计。

### 8.3 前端控制台

Vue 3 + TypeScript + Vite + Pinia + Vue Router + TanStack Vue Query + ECharts，
同源 SPA。页面按 运行总览 / 服务 / 账号 / 凭据 / 代理与模型 / 用量 / 自动化 /
治理 / 设置 分组。原始密钥与凭据不进入浏览器存储。

## 9. 可观测性与治理

- 请求遥测：记录脱敏请求事件（模型、状态、首字耗时、总耗时、token），聚合成
  用量趋势桶，支持 CSV 导出。事件按批写入，写盘失败只计数不重试，绝不影响代理请求。
- 指标快照：周期性采集账号配额/积分，保留历史窗口（可配置）。
- 审计：管理操作全部记录 `audit_events`。
- 备份：SQLite 在线备份 + restore dry-run（checksum / integrity / schema 校验）；
  真实恢复需停止 Control Plane 后离线覆盖（`offline_restore_required` 为预期结果）。

## 10. 配置与部署

- 配置来源：`.env` 启动配置 → `runtime_settings` 运行时覆盖；完整参考见
  [配置指南](../configuration.md)。
- 部署形态：官方 Docker 镜像（`ghcr.io/dmego/qoderbuddy2api`，amd64/arm64），推荐
  `docker-compose.yml` 一键启动；数据/日志/模型配置 bind mount 外挂，见
  [README · Docker deployment](../../README.md#docker-deployment)。
- 模型请求客户端：统一 base URL `http://127.0.0.1:9999/v1` + `QB2API_PROXY_API_KEY`。

## 11. 边界与非目标

- 面向单管理员本机控制台，不支持公网多租户暴露、注册计费、商业化分发。
- 不绕过验证码/风控/设备伪造；不自动抓取跨域 HttpOnly Cookie。
- 不把 Control Plane 的进程停止伪装成可恢复操作；控制面生命周期由
  Docker 容器 / 运维者维护。
- 签到与 chat 池互不耦合：签到失败不冷却模型 slot，模型故障不触发签到重试。
- 未验证的外部契约（如 `checkin-status` method）保持禁用，直至 Spike 确认。

## 13. 参考文档

| 文档 | 内容 |
| --- | --- |
| [配置指南](../configuration.md) | 密钥、`.env` 参考、远程访问、客户端示例 |
| [活跃日自动化研究](../analysis/2026-08-05-workbuddy-active-day-automation.md) | 登录自动化协议与实现边界 |
