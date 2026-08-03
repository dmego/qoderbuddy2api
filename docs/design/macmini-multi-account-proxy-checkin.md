# 2api 统一账号池、多账号代理与双端自动签到重构设计

> 状态：2026-07-23 扩展版设计基线，已获确认；截至 2026-07-24，九项本地实现任务已集成，真实 Provider 验收仍待授权
>
> 审查状态：吸收 2026-07-22 的架构审查，并加入完整本地控制台、Supervisor、Token/用量/积分监控设计；实现仍须保留真实协议 Spike 门禁
>
> 适用部署：Mac Mini 本地或 Tailscale 远程访问；常驻 Control Plane 管理独立 Proxy Worker
>
> 本文是 `docs/design` 的唯一设计方案。它合并了 CodeBuddy OAuth 池、WorkBuddy 签到、Qoder 双凭证、完整管理台和服务生命周期设计，并以当前 `2api`、本地参考工程和 2026-07-23 的 CLIProxyAPI/NewAPI/CPA-Dashboard 调研为事实依据。

### 实现与验收状态（2026-07-24）

- Control Plane/Worker、账号池、完整 Vue 管理台、设置、审计、备份和本地测试已按本设计集成；当前可复现的本地质量门禁记录在
  [进度账本](../../.superpowers/sdd/progress.md)。
- `CB-CHECKIN-01`、`QD-CHECKIN-01` 与 `AUTH-01` 仍是外部事实门禁，当前状态及脱敏记录规则以
  [Spike 结果](../spike/spike-results.md) 为准。没有明确授权、真实账号登录和脱敏结果前，不能把这些项目写为已验证。
- 第 17 节的阶段描述与第 20 节的复选框是部署/真实验收清单，不会因 mock、CI 或本地隔离 E2E 自动勾选。它们必须由实际 Mac Mini 部署与授权账号操作逐项提供证据。

## 1. 执行摘要

### 1.1 目标

把当前只支持环境变量静态 Token 的 `2api`，重构为一个运行在 Mac Mini 上的统一控制台和代理系统：

```text
2api Control Plane = 管理 UI + 管理 API + SQLite + Supervisor
2api Proxy Worker  = OpenAI/Anthropic Proxy + Provider Pools
Shared domain      = accounts + credentials + models + usage + quotas + check-in
```

客户端只需要访问统一入口 Control Plane 的 `/v1`（`/v1/models`、OpenAI base URL、Anthropic Messages 均经 `9999` 转发到 Worker）或直连 Worker 的 Anthropic 兼容端点，日常不再打开 CodeBuddy、WorkBuddy 或 QoderWork。浏览器访问 Control Plane 管理账号登录、凭据、模型、服务生命周期、Token 用量、积分快照和每日签到。停止 Worker 不会停止管理台。

### 1.2 核心决策

| 决策 | 结论 |
| --- | --- |
| 服务形态 | Python/FastAPI Control Plane 常驻；Proxy Worker 为独立受控进程，不重写 Rust 客户端 |
| 管理入口 | Control Plane 同源静态 Vue SPA + `/api/admin/*` 和 `/api/control/*` |
| 生命周期 | `ServiceSupervisor` 以 PID、启动时间、owner、内部 token 和进程组安全控制 Worker；禁止按端口盲杀 |
| Worker 边界 | Worker 只监听 loopback/internal port，Control Plane 通过内部健康/RPC 契约读取状态和下发 reload |
| 前端技术 | Vue 3 + TypeScript + Vite + Pinia + Vue Router + TanStack Vue Query + ECharts + Lucide |
| 凭据边界 | 前端发起登录或提交导入，原始 Bearer、Refresh Token、Cookie 只在后端和加密存储中出现 |
| CodeBuddy chat | `ck_`、OAuth Bearer、手动 Bearer 进入同一个 CodeBuddy 代理账号池 |
| WorkBuddy/CodeBuddy 签到 | 复用统一账号 ID，但签到凭据、状态、失败域独立于 chat |
| Qoder chat | 继续使用现有 `pt_` PAT + COSY session 代理链 |
| Qoder check-in | 默认使用桌面会话 access/refresh 双凭证；不假设 `pt_` 可以直接签到 |
| 调度器 | Control Plane 内独立 Check-inScheduler、MetricsScheduler 和 UsageRollupScheduler；按账号串行执行，单号失败不阻断其他账号 |
| 持久化 | SQLite 保存账号、purpose、配置、模型目录、Proxy Key、服务状态、审计、请求事件、用量汇总和积分快照；凭据字段使用 `cryptography` 加密 |
| 远程安全 | Proxy 与 Admin 使用不同 Key；非 loopback 管理面必须配置 Admin Key 和凭据加密主密钥。默认要求 HTTPS；若环境无法提供 HTTPS，可显式配置 `QB2API_ADMIN_COOKIE_SECURE=false`，仅限受信 Tailscale/LAN，并在 UI 持续提示传输风险 |
| 外部契约 | WorkBuddy 路径/鉴权、Qoder `pt_` 是否可签到、refresh 轮换和积分查询必须通过真实账号 Spike 后才可标记为已验证 |

### 1.3 非目标

- 不把浏览器 Cookie 自动抓取误包装成普通网页能力。跨域 `HttpOnly Cookie` 不能由前端 JavaScript 读取。
- 不把 Cookie、Bearer 或 Refresh Token 返回给浏览器、写入 URL、LocalStorage、普通日志或 `/api/config`。
- 不做验证码、CAPTCHA、扫码风控绕过、设备伪造、账号限制规避或多实例分布式控制平面。
- 不做普通用户注册、订阅套餐、充值、支付、余额计费、兑换码和商业化分发；本阶段是单管理员本地控制台。
- 不把 Control Plane 自己的进程停止按钮伪装成可恢复的“服务停止”；控制面生命周期由 launchd/systemd/人工维护，页面只控制 Proxy Worker。
- 不把 QoderWork 桌面 profile 替换、窗口切换、Tauri UI 或坐标点击带进 `2api` 常驻服务。
- 不把签到请求放进模型 `DynamicProviderPool`，也不因为签到失败盲目冷却聊天 slot。
- 不在没有实测证据时宣称 OAuth refresh、Qoder PAT 签到或 WorkBuddy Cookie 自动续期一定可用。

## 2. 研究范围与事实等级

### 2.1 当前 `2api` 事实

当前仓库是 Python `>=3.11` 的 FastAPI 项目，运行时已有 `fastapi`、`httpx`、`cryptography`、`uvicorn` 等依赖。

| 现状 | 代码位置 | 对重构的影响 |
| --- | --- | --- |
| `Settings.from_env()` 解析 `CODEBUDDY_TOKEN`、`QODER_TOKEN` 逗号列表 | `src/qb2api/config.py` | 继续兼容旧配置，启动时导入为静态账号视图 |
| `lifespan()` 创建 Provider 并在退出时关闭 | `src/qb2api/app.py` | 账号仓库、代理池、签到调度器统一在这里装配和清理 |
| CodeBuddy 请求使用 `Authorization: Bearer {token}` | `src/qb2api/providers/codebuddy.py` | OAuth Bearer 与 `ck_` 在 chat 层可共用 Provider 适配器 |
| Qoder 每个 PAT 创建一个 `QoderProvider`，首次请求建立 `QoderSession` | `src/qb2api/providers/qoder.py` | session 必须按账号缓存，并增加失效/重建边界 |
| `LoadBalancedProvider` 要求至少一个实例、按下标冷却并在流异常后 failover | `src/qb2api/providers/lb.py` | 重构为稳定 0..N `DynamicProviderPool`，修复动态结构和 partial stream 风险 |
| API Key 可选，默认 host 为 `0.0.0.0` | `src/qb2api/config.py` | 启用远程管理能力时必须强制校验管理认证 |
| `/api/config` 能写 `.env`，Token 改动要求重启 | `src/qb2api/app.py` | 旧 Token 字段保留一个兼容周期并标记 deprecated；新动态凭据只走账号 API |

### 2.2 本地参考工程

| 工程 | 当前版本/状态 | 研究结论 | 允许吸收的内容 |
| --- | --- | --- | --- |
| [`workbuddy_api`](https://github.com/akise07/workbuddy_api) | 本地 `/Users/dmego/vibeCoding/workbuddy_api`，clean，`d5de25a` | 单账号 CodeBuddy OAuth device/plugin flow；没有多账号池、签到客户端和 refresh 实现 | auth state/token URL、请求头、pending `11217`、Token 字段解析、JWT user/exp 提取思路 |
| [`qoderwork-account-switcher`](https://github.com/963072676/qoderwork-account-switcher) | 本地 `/Users/dmego/vibeCoding/qoderwork-account-switcher`，clean，`v1.1.0`，`022c1d4` | Tauri 桌面账号切换器；`quota.rs` 已实现 Qoder OpenAPI status/claim/refresh 和 COSY 额度调用 | Qoder HTTP 路径、Bearer 头、refresh 响应容错、`auth-v2.dat` 数据形状、账号摘要字段 |
| [`qoderwork_checkin`](https://github.com/GitOfUser/qoderwork_checkin) | public Python，Windows 专用 | `pyautogui` 按 2560x1440 坐标点击，要求 QoderWork 已启动；不能作为 Mac Mini 无头主路径 | 只作为“没有 HTTP 契约时的退化证据”，不移植实现 |

2026-07-23 新增管理台参考：

| 工程 | 观察到的结构 | 允许吸收的内容 | 明确不复制 |
| --- | --- | --- | --- |
| [`CLIProxyAPI`](https://github.com/router-for-me/CLIProxyAPI) | Management API、远程管理开关、API key、auth manager、配置/auth watcher、provider model registry、request-log/debug 开关 | 管理 API 与代理执行解耦、热加载、模型注册、认证来源审计 | 不把 Go 管理中心源码或外部统计服务嵌入 Python；不把远程管理默认打开 |
| [`CPA-Dashboard`](https://github.com/dongshuyan/CPA-Dashboard) | 服务控制/账号管理分栏，Provider 筛选，配额刷新，失效账号高亮，批量删除和卡片化账号详情 | 账号健康矩阵、配额刷新、批量动作、服务控制入口 | 不复制 emoji 图标、不可访问的深色对比和超长账号卡片布局 |
| [`NewAPI`](https://github.com/QuantumNous/new-api) | Dashboard、Channels、Models、Keys、Usage Logs、System Info；设置按 auth/billing/content/models/operations/security/site 分组 | 域分组、可筛选表格、用量日志、系统信息、配置来源和权限路由 | 不引入用户计费、支付、套餐、兑换码和普通用户门户 |

调研日期为 2026-07-23；源码采用 shallow clone 读取，管理台最终实现必须以 2api 自身权限、数据和安全边界为准。

参考工程只提供行为和协议线索，不把第三方代码整文件复制到 `2api`。实现须保留本地路径、参考 commit 和实测日期，方便协议变化时追溯。

### 2.3 事实等级

- **已确认**：当前源码或用户提供的真实响应直接支持。
- **参考实现**：第三方或本地工程中存在，但尚未由 `2api` 的真实账号验证。
- **验证门槛**：设计保留接口，但在进入对应实现阶段前必须通过指定 Spike。

签到接口通常没有稳定公开 SDK 契约，不能把“社区代码请求成功”写成官方兼容保证。

协议相关实现必须沿用本文事实 ID：生产代码的模块注释、测试名称或 fixture 至少引用 `CB-CHECKIN-01`、`QD-CHECKIN-01`、`AUTH-01` 中对应的一项。外部契约变化时先更新 Spike 记录和 ID 结论，再修改实现，避免文档与代码各自演化。

### 2.4 源码证据索引

| 结论 | 源码位置 |
| --- | --- |
| 当前 app 在 lifespan 静态创建多 Token Provider | `2api/src/qb2api/app.py:52` |
| 当前 CodeBuddy 固定 Bearer Header | `2api/src/qb2api/providers/codebuddy.py` 的 `_build_headers()` |
| 当前 Qoder PAT/jobToken/COSY session | `2api/src/qb2api/providers/qoder.py:97` 的 `QoderSession` |
| 当前 LB round-robin + 30 秒冷却 | `2api/src/qb2api/providers/lb.py:18` |
| CodeBuddy OAuth state/token 与 `11217` | `workbuddy_api/main.py:43`、`:171`、`:206` |
| `workbuddy_api` 明文单账号 token 文件 | `workbuddy_api/main.py:48`、`:66` |
| Qoder check-in/refresh 常量 | `qoderwork-account-switcher/src-tauri/src/core/quota.rs:32` |
| Qoder refresh 响应只取新 access | `qoderwork-account-switcher/src-tauri/src/core/quota.rs:448` |
| Qoder 非 Windows 解密 stub | `qoderwork-account-switcher/src-tauri/src/core/quota.rs:440` |
| Qoder status/claim HTTP | `qoderwork-account-switcher/src-tauri/src/core/quota.rs:553`、`:787` |
| 桌面 profile 保存/恢复边界 | `qoderwork-account-switcher/src-tauri/src/core/session.rs` |

## 3. 外部协议研究结论

### 3.1 CodeBuddy OAuth

`workbuddy_api/main.py` 的登录流程为：

```text
POST https://copilot.tencent.com/v2/plugin/auth/state?platform=CLI&nonce=<nonce>
GET  https://copilot.tencent.com/v2/plugin/auth/token?state=<state>
```

启动和轮询使用 CLI 风格请求头：

```text
User-Agent: CLI/1.0.8 CodeBuddy/1.0.8
X-Product: SaaS
X-Domain: copilot.tencent.com
X-No-Authorization: true
X-No-User-Id: true
X-No-Enterprise-Id: true
X-No-Department-Info: true
```

轮询响应：

- `code=11217`：用户尚未完成浏览器授权，保持 `pending`。
- `code=0` 且含 `data.accessToken`：保存 `accessToken`、可选 `refreshToken`、`tokenType`、`expiresIn`、`domain`、`sessionState`。
- 其他响应：转换为脱敏登录失败，不把上游原始 body 直接返回给 UI。

当前参考工程把单个账号写入明文 `token.json`，没有 refresh 实现。它只证明流程形状，不证明 refresh 契约。`2api` 必须重新实现多账号、加密存储和生命周期管理。

### 3.2 CodeBuddy/WorkBuddy 签到

用户在真实 WorkBuddy 页面观察到的路径是：

```text
POST https://www.workbuddy.cn/billing/meter/daily-checkin
https://www.workbuddy.cn/billing/meter/checkin-status  # method 未确认
```

已确认的事实只有 `daily-checkin` 的 POST method 和下述 `10001` 响应；`checkin-status` 的 URL/path 已观察到，但 method 仍属于待验证契约，不能在配置或实现中写死为 POST。

已签到时 `daily-checkin` 返回 HTTP 400，但业务 body 为：

```json
{
  "code": 10001,
  "msg": "今天已签到，请明天再来",
  "requestId": "<redacted>"
}
```

内部分类必须是：

```text
HTTP 400 + business code 10001 -> ALREADY_CHECKED_IN
```

这不是认证失败、网络失败或需要重试的错误。

公开第三方实现还出现过带 `/v2/billing/meter/*` 前缀的 CodeBuddy 域名路径，以及 `Authorization`、`X-User-Id`、`X-Enterprise-Id`、`X-Tenant-Id`、`X-Domain` 等请求头。这些路径与当前 Web 路径不同，不能静态合并。

**验证门槛 CB-CHECKIN-01：** 用当前账号 DevTools 的“复制为 cURL”保存脱敏请求摘要，确认：

1. `checkin-status` 的 method。
2. Bearer、Cookie、二者组合中哪一种是实际认证方式。
3. 两个接口是否需要相同身份头和 body。
4. `www.workbuddy.cn` 与其他域名/版本路径是否是网关重写。
5. 未签到成功、已签到 `10001`、401/403 的真实响应格式。

CB-CHECKIN-01 完成前，客户端支持配置化 path 和显式认证模式，但不把未验证路径写成永久稳定契约。

### 3.3 Qoder chat

当前 `src/qb2api/providers/qoder.py` 已经是 COSY 直连 HTTP，不再走 CLI 子进程：

```text
POST https://gateway.qoder.com.cn/algo/api/v3/user/jobToken?Encode=1
POST https://gateway.qoder.com.cn/algo/api/v2/service/pro/sse/agent_chat_generation?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1
```

`QoderSession.authenticate()` 使用 PAT 生成 job token 和 COSY 会话，把 `payload_b64`、`cosy_key`、`machine_id` 等放在进程内。当前 Provider 已按账号实例缓存 session，重构不能退回到每请求完整认证。

需要补足：

- 每个账号独立 `QoderSession`，禁止全局 session 覆盖多 PAT。
- 记录 session 建立时间、最近成功请求和失效原因。
- COSY 401/认证失败时只重建该账号 session 一次。
- 不把 COSY 派生密钥或完整 payload 返回给 UI。

### 3.4 Qoder check-in

`qoderwork-account-switcher/src-tauri/src/core/quota.rs` 的参考接口为：

```text
Base: https://openapi.qoder.com.cn
GET  /sash/api/v1/me/daily-check-in/status
POST /sash/api/v1/me/daily-check-in/claim
POST /api/v1/deviceToken/refresh
GET  /api/v2/quota/usage
```

请求特征：

```text
Authorization: Bearer <device/session access token>
User-Agent: QoderWork
Content-Type: application/json       # claim body 为 {}
```

参考源码的 `AuthV2Data` 包含 `token`、`refresh_token`、`expires_at` 和 user。`get_valid_token()` 在 access 距离过期不足一小时尝试 refresh；响应支持 `device_token` 或 `token` 字段。

必须保留的限制：

1. 参考 `refresh_token()` 只返回新 access，虽然结构体接受可选新 `refresh_token`，当前实现没有持久化轮换值。
2. 非 Windows 平台的 `decrypt_auth_data()` 是错误 stub。因此不能承诺 Mac Mini 直接解析加密 `auth-v2.dat`。
3. `session.rs` 保存完整桌面状态，但 headless 签到只需要经过验证的 access/refresh，不上传完整 profile。
4. `qoderwork_checkin` 是 Windows 坐标点击，要求桌面端运行，不是服务主路径。

**验证门槛 QD-CHECKIN-01：**

| 实验 | 通过标准 | 失败后的设计 |
| --- | --- | --- |
| `pt_` 直接调用 status/claim | 200 且语义正确 | 保持 chat/check-in 双凭证 |
| jobToken 响应安全字段调用 claim | 200 且语义正确 | COSY chat session 不作签到凭据 |
| access 过期后 refresh 再 claim | 成功 | 启用自动 refresh |
| 多次 refresh 是否轮换 refresh token | 轮换值可继续用 | 有新值则持久化，无新值保留旧值 |

QD-CHECKIN-01 完成前，Qoder 模型同时允许 `chat.pat` 和 `checkin.access/refresh`，不能因猜测相同而缩减字段。

## 4. 总体架构

### 4.1 逻辑视图

```text
                         Cursor / Claude Code / Codex
                                      |
                           OpenAI / Anthropic API
                                      v
  +-------------------------------------------------------------------+
  | Control Plane (persistent, admin-only)                            |
  |                                                                   |
  |  Vue SPA + FastAPI Admin API + SQLite + Credential Vault          |
  |  Account/Model/Usage/Check-in repositories                        |
  |  MetricsScheduler + UsageRollupScheduler + CheckinScheduler       |
  |  ServiceSupervisor  <---- loopback control RPC ----> Worker       |
  +-------------------------------------------------------------------+
                                      |
                            launch / stop / reload
                                      v
  +-------------------------------------------------------------------+
  | Proxy Worker (independent, loopback bind only)                    |
  |                                                                   |
  |  OpenAI / Anthropic compatibility API                             |
  |  DynamicProviderPool(codebuddy, 0..N)                             |
  |  DynamicProviderPool(qoder, 0..N)                                 |
  |  request telemetry + model catalog snapshot                       |
  +-------------------------------------------------------------------+
                                      |
                  CodeBuddy / WorkBuddy / Qoder upstreams
```

### 4.2 责任边界

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| `ControlPlaneApp` | 管理 API、SPA、SQLite、凭据、审计、调度、Supervisor；即使 Worker 停止也可访问 | 处理模型请求、直接代理上游 chat |
| `ProxyWorkerApp` | 兼容 OpenAI/Anthropic API、Provider Pool、请求级 telemetry、模型快照 | 修改账号/凭据、签到、启动其他进程 |
| `ServiceSupervisor` | 校验 Worker 命令、启动/停止/重启/健康检查、PID 与进程组保护、内部 token 轮换 | 持有上游凭据或代理请求 |
| `AccountRegistry` | 账号元数据、purpose 状态、稳定 ID、能力摘要、动态快照 | 直接发上游 HTTP |
| `CredentialVault` | 加密保存/读取 Secret、版本化、原子更新、权限检查 | 决定业务能力、向 UI 返回 Secret |
| `CredentialResolver` | 按 provider/account/purpose 返回临时凭据，单飞 refresh、缓存失效 | 返回凭据给浏览器 |
| `DynamicProviderPool` | chat 账号选择、冷却、0..N slot 热替换、未输出首 chunk 前 failover | 签到顺序、管理 API、流式开始后的重放 |
| `CheckinService` | 单账号/批次签到、分类、落库、账号隔离 | 计算下一次调度时间 |
| `CheckinScheduler` | 时区、每日窗口、补偿、批次锁、生命周期 | 构造 HTTP 请求 |
| `MetricsScheduler` | 周期性 token/积分/配额刷新、限频、错误退避 | 修改凭据或触发代理请求 |
| `UsageRollupScheduler` | 请求事件聚合、日/月统计、保留策略 | 生成账单、扣费、外部计费 |
| `OAuthBroker` | CodeBuddy OAuth HTTP 和 flow 状态 | 多账号轮询或 UI 渲染 |
| `Admin UI` | 展示脱敏状态、触发动作、收集输入、服务控制 | 读取跨域 Cookie、保存明文 Secret、执行 shell |

### 4.3 Supervisor 生命周期与 Worker 状态机

Control Plane 是唯一常驻服务；Worker 是受其管理的短生命周期子进程。Supervisor 不根据端口盲杀进程，而是维护并校验以下运行记录：`worker_pid`、`process_start_time`、`process_group_id`、`owner_instance_id`、`internal_auth_version`、`desired_state`、`observed_state`、`last_exit_code`、`last_error`。

```text
STOPPED -> STARTING -> HEALTHY -> STOPPING -> STOPPED
              |           |
              v           v
            FAILED <--- DEGRADED
```

- `STARTING` 必须先完成内部 token 握手、版本兼容检查、模型快照加载和 `/internal/health/ready`，否则进入 `FAILED` 并保留诊断信息。
- `HEALTHY` 只表示 Worker 可接受请求；上游账号全部不可用时为 `DEGRADED`，仍允许健康检查和管理 API 读取状态。
- `STOPPING` 先将 Worker 标记为 draining，拒绝新请求，等待 in-flight 请求和流式响应归零；达到可配置 grace period 后发送 SIGTERM，超时才向已验证的同一进程组发送 SIGKILL。
- 判断目标进程必须同时匹配 PID、启动时间、owner instance id 和内部握手 token。任何字段不匹配都不得发送终止信号。
- `start` 在已有同一 owner 的健康 Worker 时幂等返回；`restart` 采用 drain -> stop -> start；`reload` 只替换凭据/模型快照，不改变进程 PID，失败时保留旧快照。
- UI 只能控制 Worker；Control Plane 自身由 launchd/systemd 或人工运维管理，不能提供“停止管理台”按钮。

### 4.4 Control Plane 与 Worker 边界

- Worker 只绑定 `127.0.0.1:<worker_port>`，生产环境不直接暴露到 LAN/Tailscale；远程访问统一进入 Control Plane 的管理会话。
- Control Plane 启动 Worker 时通过一次性内部 token 和受限环境变量传递只读配置位置。Worker 通过内部 RPC 请求经版本化的 `runtime_snapshot`，不直接打开管理 API 的 session cookie。
- Worker 的 `/internal/*` 路由只接受 loopback 请求和内部 token；不得复用 `QB2API_ADMIN_KEY` 或 `QB2API_PROXY_API_KEY`。
- Proxy API Key 只进入 Worker 的 OpenAI/Anthropic 路由；Admin Key 只进入 Control Plane 的管理会话。旧 `QB2API_API_KEY` 兼容期只能映射为 Proxy 权限。
- Worker 将 request event、模型发现结果、账号健康结果发送给 Control Plane；发送失败不能阻塞模型响应，内存队列满时按保留策略丢弃低优先级 telemetry 并计数。

### 4.5 并发、缓存与安全边界

- SQLite 访问统一通过异步 Repository；实现可选 `aiosqlite` 或 `asyncio.to_thread`，不得在请求协程直接执行同步连接和解密。
- SQLite 开启 `WAL`、`busy_timeout`、`foreign_keys=ON`；写事务只包含单个领域操作，跨表更新使用显式事务。
- 凭据缓存键为 `(provider, account_id, purpose, credential_version)`，轮换或禁用后按版本失效；SSE 请求不得为每个 chunk 查询 SQLite。
- Proxy 并发；同一账号同一 purpose 的 refresh 由单飞锁保护。签到批次全局互斥，账号默认串行并带 jitter。
- 每个 provider 始终注册一个支持 0..N slot 的 `DynamicProviderPool`；slot 以 `(provider, account_id)` 为稳定键。退役 slot 停止接收新请求，在 in-flight 归零后关闭；无法可靠跟踪时至少保留至 Worker 退出。
- 只有向下游输出第一个 chunk 之前允许 refresh/failover；已输出任意 chunk 后发生异常，只记录失败并终止当前流，禁止切换账号重放。

## 5. 统一账号模型

### 5.1 稳定标识

主键为 `(provider, account_id)`：

```text
provider = codebuddy | qoder
account_id = <provider-prefix>-<random-uuid-or-opaque-id>
```

`account_id` 只允许 `[A-Za-z0-9._-]`。OAuth、manual import 或 promotion 创建账号时生成随机稳定 ID；后续 Token/PAT/refresh 轮换只增加 `credential_version`，绝不改变账号 ID。上游稳定 identity 只保存 keyed hash，Secret fingerprint 只保存在 credential 内部字段，二者均不返回 UI。

旧环境变量 Token 转换为不含 Secret 的临时来源槽位 ID：

```text
CODEBUDDY_TOKEN[0] -> codebuddy/cb-env-0
QODER_TOKEN[0]     -> qoder/qd-env-0
```

静态 Secret 仍来自环境变量，不自动复制进数据库；UI 显示 `source=env` 且不可删除。槽位 ID 只保证“同一变量下标”稳定：轮换同一位置的 Token 不改 ID，重排列表会改变槽位对应的真实账号。因此 env slot 只承担 legacy chat，不允许直接挂接持久签到凭据、长期备注或审计身份。

需要长期身份或 check-in 关联时，管理员显式执行 promotion：后端生成新的随机 `account_id`，把当前 env chat Secret 加密写入 Vault，创建动态 purpose，再由同一事务后的 pool update 使动态账号 shadow 原 env slot。promotion 是受 Admin Key 保护的一次性操作，不修改 `.env`，也不把 Secret 返回浏览器。

Token hash 只能作为 keyed HMAC 内容指纹，用于内部去重、promotion 和轮换诊断，不能作为主键、日志字段或 UI 身份。没有凭据主密钥时不持久化指纹。

### 5.2 元数据和状态

账号只保存全局元数据；业务开关、状态和错误全部放在 purpose 记录中：

```json
{
  "provider": "codebuddy",
  "account_id": "cb-alice",
  "label": "alice",
  "source": "oauth",
  "enabled": true,
  "summary_status": "action_required",
  "purposes": {
    "chat": {
      "enabled": true,
      "status": "active",
      "verification_status": "verified",
      "capabilities": ["proxy.chat", "credential.refresh"],
      "verified_at": "2026-07-22T00:00:00Z"
    },
    "checkin": {
      "enabled": true,
      "status": "needs_reauth",
      "verification_status": "verified",
      "capabilities": ["checkin.workbuddy"],
      "verified_at": "2026-07-21T00:00:00Z"
    }
  },
  "user_id": "<masked>",
  "created_at": "2026-07-22T00:00:00Z",
  "updated_at": "2026-07-22T00:00:00Z"
}
```

purpose 固定为 `chat | checkin`。运行状态为 `unconfigured`、`needs_import`、`active`、`expired`、`needs_reauth`、`disabled`、`invalid`；验证状态独立为 `not_required`、`unverified`、`verified`、`rejected`。新增业务 purpose 时增加记录，不给 `accounts` 表加成对状态列，也不能用运行状态代替协议验证状态。

`summary_status` 只由查询层为 UI 派生，不落库、不参与路由或调度：全局关闭为 `disabled`；任一启用 purpose 需要人工处理为 `action_required`；否则任一 purpose 可用为 `active`；其余为 `pending`。Proxy 和 check-in 的选择器只读取各自 purpose。例如账号可以是 `chat=active`、`checkin=needs_reauth`，签到失败不能踢出 chat。

### 5.3 purpose 级凭据

```text
codebuddy/chat:
  access_token, refresh_token?, expires_at, token_type

codebuddy/checkin:
  mode = inherit_chat | bearer | cookie | bearer_cookie
  access_token?, refresh_token?, expires_at?, cookie?
  uid?, enterprise_id?, tenant_id?, domain?

qoder/chat:
  pat
  COSY session: 默认仅进程内

qoder/checkin:
  access_token, refresh_token, expires_at, user_agent=QoderWork
```

同一 Secret 可以用 `credential_ref` 复用加密值，避免双写；两个 purpose 的状态、refresh lock 和失败次数仍分开。`credentials` 只表示可解密的材料，不承载业务健康状态。

### 5.4 Capability Matrix

能力包括 `proxy.chat`、`checkin.workbuddy`、`checkin.qoder`、`credential.refresh`、`credential.cookie`。

- 配置声明表示“允许尝试”，`verification_status` 初始为 `unverified`，不表示已验证。
- 首次成功的对应 probe/upstream 行为把验证状态改为 `verified` 并写入 `verified_at`；明确协议不匹配改为 `rejected`。
- 401/403 把对应运行状态改为 `needs_reauth`，但不抹掉已经成立的协议验证；明确协议不匹配才把验证状态改为 `rejected`。两者都不能删除其他 purpose 的凭据。
- capability 和状态写入 `account_purposes`；调用方不得从账号汇总状态反推某项能力。
- 为保持旧 env chat 行为，legacy static chat 可使用 `verification_status=not_required` 进入 Proxy；check-in scheduler 只选择 `enabled=true AND status=active AND verification_status=verified` 的 purpose。

## 6. 数据持久化与安全

### 6.1 SQLite 元数据

使用 `aiosqlite` 的单连接异步 Repository，不引入 ORM，也不允许请求协程直接调用同步 `sqlite3`。实现时在 `pyproject.toml` 增加 `aiosqlite>=0.20.0` 运行时依赖：

```text
${QB2API_DATA_DIR:-./data}/qb2api.sqlite3
```

逻辑表：

```text
accounts
  provider, account_id, label, source, enabled,
  masked_identity, identity_hash, created_at, updated_at
  PK(provider, account_id)

account_purposes
  provider, account_id, purpose, enabled, status, verification_status,
  capabilities_json, verified_at, expires_at,
  last_success_at, failure_count, last_error, updated_at
  PK(provider, account_id, purpose)
  FK(provider, account_id) -> accounts ON DELETE CASCADE

credentials
  id, provider, account_id, purpose, mode,
  encrypted_payload, payload_version, credential_version,
  fingerprint_hmac, expires_at, has_refresh_token, updated_at
  UNIQUE(provider, account_id, purpose)

checkin_runs
  run_id, local_date, timezone, started_at, finished_at,
  status, trigger, error_message
  PK(run_id)

checkin_attempts
  run_id, provider, account_id, outcome, http_status,
  business_code, request_id, attempts, timestamps, redacted_error
  PK(run_id, provider, account_id)

checkin_daily_state
  provider, account_id, local_date, timezone, terminal_outcome,
  last_run_id, updated_at
  PK(provider, account_id, local_date, timezone)

oauth_flows
  state_hash, provider, label, created_at, expires_at, status, account_id

admin_sessions
  session_hash, csrf_hash, created_at, last_seen_at, expires_at, revoked_at
  PK(session_hash)

runtime_settings
  key, value_json, value_version, source, updated_at, updated_by
  PK(key)

service_runtime
  service_name, desired_state, observed_state, worker_pid,
  process_start_time, process_group_id, owner_instance_id,
  internal_auth_version, started_at, stopped_at, last_health_at,
  last_exit_code, last_error, updated_at
  PK(service_name)

proxy_api_keys
  key_id, name, key_hash, scopes_json, enabled, created_at,
  last_used_at, expires_at, revoked_at
  PK(key_id)

model_catalog
  provider, model_id, display_name, capabilities_json, source,
  enabled, last_seen_at, metadata_json
  PK(provider, model_id)

request_events
  event_id, request_id, provider, account_id, model_id, protocol,
  status, http_status, input_tokens, output_tokens, latency_ms,
  started_at, finished_at, error_code, redacted_error
  PK(event_id)

usage_rollups
  bucket_start, bucket_kind, provider, account_id, model_id,
  request_count, success_count, error_count, input_tokens,
  output_tokens, latency_p50_ms, latency_p95_ms, updated_at
  PK(bucket_start, bucket_kind, provider, account_id, model_id)

account_metric_snapshots
  provider, account_id, metric_kind, metric_value_json,
  observed_at, expires_at, status, last_error
  PK(provider, account_id, metric_kind)

audit_events
  event_id, actor_type, actor_id, action, resource_type,
  resource_id, result, metadata_json, created_at
  PK(event_id)

backup_runs
  backup_id, path, schema_version, started_at, finished_at,
  status, size_bytes, sha256, error_message
  PK(backup_id)
```

OAuth 原始 state 只在进程内或加密保存，数据库最多保存 hash，防止重放。管理 session 和 CSRF 原值只存在于 cookie/响应及进程内，数据库只保存 hash；Admin Key 轮换时撤销全部 session。

表约束和保留策略：

- `runtime_settings` 只保存经过 schema 校验的非 Secret 运行参数。Admin UI 修改设置时以 `value_version` 做乐观并发控制，提交后由 Supervisor 原子触发 scheduler reschedule 或 Worker reload；环境变量仍可作为只读 fallback，不能形成双写。
- `service_runtime` 是 Supervisor 的事实记录，不用端口扫描结果替代。启动、停止、重启和异常退出都写审计事件；Worker 健康探测只更新 `last_health_at` 和 observed state。
- `proxy_api_keys` 保存独立于 Admin Key 的 hash/scopes。MVP 默认只创建一个 Proxy Key，但 schema 从第一天支持轮换、撤销和过期；数据库不保存可逆 Key。
- `model_catalog` 是 Worker 发现结果和管理员覆盖的合并视图。`source=provider|manual|definition`，模型禁用只影响路由，不删除历史 usage。
- `request_events` 只保存脱敏 request id、模型、状态、耗时和 token 数。请求正文、Authorization、Cookie、Prompt、响应内容和上游原始错误禁止落库。事件写入失败不能让模型请求失败。
- `usage_rollups` 从 request events 批量聚合；默认保留 90 天明细、24 个月日汇总，超期由后台清理任务分批删除并记录审计。
- `account_metric_snapshots.metric_kind` 至少包含 `token_status`、`points`、`quota`、`checkin_summary`。积分/配额未知或过期时保留 `status=stale|unavailable`，不能伪造 0。
- `audit_events` 只记录 actor、动作、资源复合 ID、结果和脱敏元数据。任何 Secret、请求头、完整 URL query 和上游响应原文都属于禁止字段。
- 备份是显式管理动作，数据库和凭据主密钥必须分开备份；`backup_runs` 只记录路径、哈希和状态，不把密钥打包进 HTTP 下载响应。

### 6.2 SQLite 异步边界、事务与缓存

Repository 在 lifespan 中创建一个长期 `aiosqlite.Connection`，所有 SQL 都通过 Repository async API 串行进入其 worker thread；Handler、Provider、Scheduler 和 Vault 不持有裸连接。连接初始化必须执行并验证：

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
PRAGMA synchronous=NORMAL;
```

事务规则：

1. 写事务使用显式 `BEGIN IMMEDIATE`，只包含必要 SQL 和本地序列化；禁止在事务内等待 OAuth、refresh、status、claim 或任何上游 HTTP。
2. refresh 流程是“读版本 -> 释放 DB -> 调上游 -> 短事务按旧 `credential_version` 条件更新”；版本冲突时丢弃旧结果并重新 resolve，不能覆盖较新的 refresh token。
3. 账号/credential/purpose 的同一业务修改在一个事务中提交；提交成功后才发布 registry invalidation event。失败时内存 snapshot 不得先行变化。
4. 签到 attempt 与 daily state 在同一短事务落库；单账号失败不回滚其他已完成账号。
5. schema migration 在 Scheduler 和 Proxy pool 启动前独占完成；不在运行中隐式迁移。

`CredentialResolver` 每次请求仍被调用，但不代表每次访问 SQLite。它维护以 `(provider, account_id, purpose)` 为键的内存 `CredentialSnapshot`，包含解密后的最小凭据和 `credential_version`：

- 启动/首次使用通过 async Repository 加载，解密若可能阻塞则使用 `asyncio.to_thread`。
- 管理更新或 refresh 提交后递增 `credential_version`，提交事件精确失效对应缓存；下一次 resolve 重新加载。
- purpose refresh lock 包围缓存检查和 refresh 协调，但不包围普通上游 chat 请求。
- 缓存不设“永远有效”的时间假设；过期判断、skew 和 version 都必须通过。
- 进程退出或账号退役时清除对应快照引用，日志和指标不暴露 payload/fingerprint。

因此 Provider 的逐请求 resolver 是 O(1) 内存路径，SQLite 写入、解密和锁等待不会直接阻塞 FastAPI 事件循环。

### 6.3 Secret 加密

启用动态账号、OAuth 或 check-in import 时必须配置：

```ini
QB2API_CREDENTIAL_KEY=<base64-url-safe-fernet-key>
```

使用现有 `cryptography` 的 Fernet 或等价 AEAD：

1. 启动校验 key 格式。
2. 每个 payload 单独加密，数据库只保存 ciphertext、版本、更新时间。
3. 读写使用事务；先写新 ciphertext，再更新元数据。
4. 日志只输出 `provider/account_id/purpose`。
5. 换主密钥走显式迁移，不静默覆盖旧数据。

Mac Mini 可把 key 放进 Keychain，但不是第一阶段必要依赖。无 key 时禁止新增持久化 Secret，保留 env-only 代理兼容行为。

### 6.4 文件与网络安全

- `QB2API_DATA_DIR`、数据库和备份加入 `.gitignore`。
- POSIX 目录建议 `0700`，数据库和临时导入文件 `0600`。
- 启用管理 UI、动态凭据或签到时必须配置 Admin Key；env-only proxy 可继续兼容无 Proxy Key 模式。
- 远程管理优先使用 HTTPS（Tailscale Serve/受信反向代理）。无法部署 HTTPS 时，可显式配置 `QB2API_ADMIN_COOKIE_SECURE=false` 允许受信 Tailscale/LAN HTTP；这属于管理员主动接受风险的降级模式，禁止公网暴露，并应限制监听地址和主机防火墙。
- upstream host 使用 provider allowlist，管理请求不能提供任意 URL，防止 SSRF。
- 导入请求限制 body、速率和并发；完成后清除前端表单和后端临时变量。
- `/api/config` 只返回掩码配置。

## 7. 管理认证与前端 UI

### 7.1 路由认证矩阵与 bootstrap

当前中间件除少量 public path 外统一要求 Bearer。重构后必须先按 method + path 分类，不能简单把整个 `/admin` 或 `/api/admin` 加入白名单：

```text
PUBLIC_EXISTING
  GET /health, /version, /docs, /openapi.json

PUBLIC_ADMIN_BOOTSTRAP
  GET  /admin
  GET  /admin/*                 # 只返回 SPA shell/fallback
  GET  /static/admin/*          # 只含版本化静态资源
  POST /api/admin/session       # 路由内部校验 Admin Key 和限流

ADMIN_PROTECTED
  其余 /api/admin/*
  -> Authorization: Bearer <QB2API_ADMIN_KEY>，或有效 admin session

PROXY_PRIVATE
  /v1/*、/v1/messages、/api/tags、/api/show、/api/v1/models
  -> Authorization: Bearer <QB2API_PROXY_API_KEY>

ADMIN_LEGACY_PRIVATE
  /api/config
  -> Authorization: Bearer <QB2API_ADMIN_KEY>；不接受 cookie session
```

公开的 UI shell、JavaScript 和 CSS 不能内嵌账号数据、配置快照、CSRF token 或 Secret。`POST /api/admin/session` 只是在全局中间件层可达，不代表匿名成功；它必须在路由内部使用 constant-time comparison 校验 Admin Key。

Proxy 和 Admin 使用不同根密钥：`QB2API_PROXY_API_KEY` 只能调用 Proxy/模型兼容路由，`QB2API_ADMIN_KEY` 才能建立管理 session 或调用管理 API。生成的 session 是独立随机凭据，权限只覆盖 `/api/admin/*`；兼容路由 `/api/config` 只接受 Admin Key Bearer，避免扩大 cookie path。API Key Bearer 请求不依赖 cookie，因浏览器不会自动附加该 header，可不做 CSRF；cookie 认证的变更请求必须校验 CSRF。

兼容规则：旧 `QB2API_API_KEY` 在迁移期只作为 `QB2API_PROXY_API_KEY` 的别名，不再获得 Admin 权限；只配置旧 Key 而启用管理 UI、动态凭据或签到时，启动必须拒绝并提示配置 `QB2API_ADMIN_KEY`。`QB2API_PROXY_API_KEY` 与 `QB2API_ADMIN_KEY` 不能相同。旧管理客户端必须迁移到 Admin Key，不能为了兼容而让 Proxy Key 继续写账号或凭据。

### 7.2 管理会话与 Cookie

管理 UI 登录流程：

1. `POST /api/admin/session` 提交 Admin Key。
2. 后端创建 256-bit 随机 session，仅持久化 hash，并设置 `HttpOnly; SameSite=Lax; Path=/api/admin` cookie。
3. 响应体返回独立 CSRF token；所有 cookie 认证的 `POST/PATCH/PUT/DELETE` 使用 header 回传。
4. 单个部署最多保留 5 个并发 session，默认绝对 TTL 12 小时；超额时撤销最旧 session。
5. 重新提交 Admin Key 或执行显式 re-auth 时创建新的 session ID + CSRF token，并撤销旧 session；不做无限滚动延长绝对 TTL。
6. logout 撤销当前 session，logout-all 撤销全部 session；Admin Key 轮换也撤销全部 session。撤销记录写 `revoked_at`，后续请求一律拒绝。

Admin Key 只从部署 Secret/环境读取，不保存进数据库。服务每次启动都先撤销未过期的旧 session，因此 session 不跨进程重启；Admin Key 轮换通过受控重启生效，也天然使全部旧 cookie 失效。

登录失败默认按来源地址执行“5 次/5 分钟，锁定 15 分钟”，并增加部署级突发上限；成功登录不回显 Admin Key。来源地址默认取 socket peer，只有来自允许网络的显式受信反向代理才接受 forwarded headers。限流计数、请求 body 和认证失败日志都不得保存任何 Key。session 采用绝对 TTL 12 小时 + 空闲 TTL 60 分钟，`last_seen_at` 最多每分钟触碰一次，避免每个管理请求都写数据库。

`QB2API_ADMIN_COOKIE_SECURE` 契约：

- `true`：只接受 HTTPS 管理请求，始终设置 `Secure`。
- `auto`（默认）：直接 HTTPS 或受信代理传入的 HTTPS scheme 设置 `Secure`；loopback HTTP 不设置；远程 HTTP 拒绝创建 session。
- `false`：显式关闭 Cookie 的 `Secure` 标志，允许 loopback 或受信 Tailscale/LAN HTTP。该值代表部署者主动接受应用层无 TLS 的风险；管理台必须显示醒目警告，服务不得因此放宽 Admin Key、CSRF、限流或监听/防火墙边界。

因此默认生产拓扑仍是 `https://<tailscale-host>/admin`。确实无法提供 HTTPS 时，`http://<tailscale-ip>:9999/admin` 只在 `QB2API_ADMIN_COOKIE_SECURE=false`、端口仅对 tailnet/受信 LAN 可达且用户理解风险时受支持。`auto` 永远不会静默降级远程 HTTP。不把 API Key 放进 URL、LocalStorage 或 SessionStorage。

### 7.3 UI 页面

管理台采用 `Vue 3 + TypeScript + Vite + Pinia + Vue Router + TanStack Vue Query + ECharts + Lucide`。Vite 仅用于构建，生产部署仍由 Control Plane 同源提供静态产物，不增加 Node 常驻进程。匿名访问只加载登录 shell；登录后才请求管理数据。

视觉定位是桌面优先、浅色且高密度的本地基础设施控制台，不是极简营销页，也不复制参考工程的代码、品牌或配色。视觉信息架构采用 2api 自身的固定侧栏、工作区 Header、筛选表格、详情抽屉与危险操作确认，并保留单管理员安全边界。左侧固定导航按 `运行`、`账号池`、`代理与模型`、`自动化`、`治理` 五个业务域分组；中间工作区使用紧凑页面标题、状态摘要、筛选工具栏、表格和详情抽屉。主操作使用蓝色，绿色/琥珀色/红色分别表示成功、注意和失败，状态同时配合文字与图标，不能只靠颜色传达。避免渐变背景、装饰性大卡片、夸张标题、嵌套卡片和单一色系。

桌面端侧栏支持折叠并保留 icon tooltip；窄屏改为显式打开/关闭的抽屉和遮罩，不把导航挤进内容区。数据表在窄屏保持可横向滚动，详情、确认和危险动作进入全屏可访问抽屉/对话框。所有图标按钮均有可访问名称，辅助文字、导航分组与状态文字达到 WCAG AA 对比度；Playwright 覆盖桌面和窄屏导航，Lighthouse 在两种设备配置下验证可访问性、最佳实践、SEO 与 agentic browsing。

```text
/admin/login
  Admin Key 登录、会话状态、HTTPS/loopback 安全提示

/admin/overview
  Worker 状态、可用账号、模型数、今日请求/token、错误率、积分摘要
  24h 请求/token 趋势、Provider 健康矩阵、近期签到和异常事件

/admin/service
  Worker start/stop/restart/reload、desired/observed state、PID/uptime/version
  健康检查、启动日志尾部、draining/in-flight、最近退出原因

/admin/accounts
  provider/source/status/purpose/标签筛选、批量启停/探测/签到
  账号表格、token 状态、积分/配额、最近请求/签到、错误摘要

/admin/accounts/:provider/:accountId
  Overview、Credentials、Proxy health、Quota/points、Check-in、Events
  label/目的启停、重新登录、刷新、promotion、删除、历史趋势

/admin/accounts/add
  CodeBuddy OAuth/manual Bearer、Qoder PAT、Qoder check-in access+refresh
  WorkBuddy Cookie/身份头（仅实测需要时显示）；分步验证后才提交

/admin/credentials
  凭据类型、版本、过期时间、refresh 能力、验证状态、最后轮换
  只允许轮换/撤销/重新授权，永不展示原文或 fingerprint

/admin/models
  Provider/模型/能力/来源/可用账号/状态；启停、探测、刷新目录
  模型详情含 usage、延迟、错误率和路由账号，不编辑任意上游 URL

/admin/usage
  request/token/success/error/latency 趋势，按时间、Provider、模型、账号筛选
  事件表和 request detail 只显示脱敏元数据，可导出 CSV 汇总

/admin/checkin
  scheduler 状态、下次执行、今日批次、账号结果、积分变化
  手动全量/指定执行、失败重试、历史 runs/attempts、needs_reauth

/admin/settings
  General、Proxy、Scheduler、Monitoring、Retention、Security、Backup
  显示 value/source/apply-mode/version；支持验证后保存与原子应用

/admin/audit
  管理登录、账号/凭据、服务控制、设置、备份操作的脱敏审计记录
```

全局交互规则：

- Header 固定显示 Worker 状态、活动请求、指标快照和今日请求；状态变化通过轮询更新，不要求手动刷新整页。
- start/stop/restart、删除账号、撤销凭据、恢复备份属于危险动作，必须有目标、影响和不可逆性确认；按钮在请求期间锁定并显示真实进度。
- 列表页提供搜索、Provider/status/purpose 筛选、分页、空状态、错误状态、骨架屏、批量选择和详情抽屉。窄屏数据表使用横向滚动，移动端保留查看、签到、重新授权等必要动作。
- 任何 mutation 都显示明确的成功/失败反馈并使相关 Query 精确失效；不能乐观伪造 Worker 已启动、设置已生效或凭据已验证。
- 表单错误与字段关联，异步流程可恢复；OAuth poll、Worker starting/draining、签到批次和备份过程离开页面后仍可从服务端状态恢复。
- 图表必须有文字摘要、可访问 legend 和无数据状态。数值统一显示采样时间；积分/配额过期标记为 stale，不显示为 0。
- 使用 Lucide 图标和 tooltip；图标按钮有 `aria-label`。键盘可完成导航、筛选、对话框和抽屉操作，焦点可见，正文/控件达到 WCAG AA 对比度。

### 7.4 运行设置编辑与应用

`GET /api/admin/settings` 返回 schema 化设置组，`PATCH /api/admin/settings` 只接受白名单 key、期望 `value_version` 和经过类型/范围校验的值。每项标明：

```json
{
  "key": "checkin.schedule.at",
  "value": "00:10",
  "source": "runtime",
  "value_version": 4,
  "apply_mode": "scheduler_reschedule",
  "restart_required": false
}
```

应用模式固定为 `immediate`、`scheduler_reschedule`、`worker_reload`、`worker_restart`、`control_restart_required`。Control Plane 先提交新版本，再执行应用动作；应用失败时保留新值但标记 `pending_apply/error`，不得向 UI 报告已生效。对于 scheduler reschedule，先构建并校验新 scheduler，再原子替换旧实例；对于 Worker reload/restart，由 Supervisor 返回 operation id，UI 跟踪到 terminal state。

### 7.5 CodeBuddy OAuth UI 流程

```text
UI -> POST /api/admin/auth/codebuddy/start {label}
2api -> copilot plugin/auth/state
UI <- {flow_id, auth_url, expires_at}
UI -> 在腾讯页面打开 auth_url
UI -> POST /api/admin/auth/codebuddy/poll {flow_id}
2api -> copilot plugin/auth/token
11217 -> pending
success -> 加密保存、创建账号、刷新 Proxy pool
UI <- AccountView，不含 token
```

flow 有 15 分钟 TTL、一次性消费、state hash 和并发 poll 限制。后台不读取腾讯页面 Cookie。

### 7.6 上游凭据 Cookie 边界

普通 Web UI 无法跨域读取 `workbuddy.cn` 的 `HttpOnly Cookie`，因此：

1. Bearer-first：先用 OAuth access token 验证签到。
2. 若真实 cURL 证明需要 Cookie，用户在 HTTPS/loopback 管理页手动导入，后端加密保存。
3. 浏览器扩展、CDP 或 Tauri helper 属于未来独立项目，不阻塞主方案。

前端只显示 `cookie_available=true` 和掩码。

## 8. Proxy 账号池

### 8.1 Pool API

账号池对 Provider 层提供最小接口：

```python
class AccountPool(Protocol):
    async def snapshot(self, purpose: str) -> list[AccountSlot]: ...
    async def credential(
        self, provider: str, account_id: str, purpose: str
    ) -> Credential: ...
    async def mark_success(
        self, provider: str, account_id: str, purpose: str
    ) -> None: ...
    async def mark_failure(
        self, provider: str, account_id: str, purpose: str, failure: Failure
    ) -> None: ...
    async def refresh(
        self, provider: str, account_id: str, purpose: str
    ) -> Credential: ...
```

`AccountPool` 不发上游 HTTP，也不返回给 UI。`snapshot()` 读取 Registry 发布的内存快照，不在请求路径查询 SQLite。每次上游尝试按 purpose 获取最新凭据，避免 refresh 后旧 Provider 永久持有旧 Token。流式请求在一次上游尝试开始时取得不可变凭据快照；401 refresh 后的新尝试必须重新 resolve。

Proxy 的 round-robin、cooldown 和瞬时健康状态只保存在 pool 内存；普通成功不逐请求写数据库。`last_success_at` 使用有界频率合并写入，`needs_reauth/disabled/invalid` 等持久状态转换才立即提交 Repository。这样状态可观测性不会把高频 chat 变成 SQLite 写放大。

### 8.2 稳定的 `DynamicProviderPool(0..N)`

CodeBuddy 和 Qoder 在 lifespan 中各注册一个长期稳定的 `DynamicProviderPool`，不再根据账号数切换 absent/direct/LB 三种对象结构：

```text
0 slot  -> pool 仍在 Registry；请求返回 503 provider_unavailable
1 slot  -> 同一个 pool 选择唯一 slot
N slot  -> 同一个 pool round-robin/failover
```

`ProviderRegistry` 和路由层只持有稳定 pool 引用，账号增删不调用 registry `replace()`/`unregister()`。模型 catalogue 从 `model_definitions` 构建一次，账号可用性在请求和 `/v1/models` 查询时读取 `pool.has_available_slots`：加入第一个账号后模型立即可见，删除最后一个账号后模型从 available list 隐藏；并发删除竞态下已解析请求得到明确 503，而不是 `unknown model`。

pool 内部状态全部按 `SlotKey(provider, account_id)` 保存，round-robin cursor、cooldown、失败次数和日志不得按数组下标关联账号。active slots 使用不可变 tuple/map snapshot；更新 snapshot 不改变 pool 对象身份。

### 8.3 CodeBuddy chat

启动/重建：

1. 读取 `CODEBUDDY_TOKEN`，生成不可删除的 static slots。
2. 连接 `accounts + account_purposes`，读取全局 enabled 且 `purpose=chat` 可用的动态 slots。
3. 每个 slot 创建一个轻量 `AccountBackedCodeBuddyProvider`。
4. 不论得到 0、1 或 N 个 slot，都调用稳定 pool 的 `update_slots()` 替换 active snapshot。

`AccountBackedCodeBuddyProvider` 构造函数只能缓存 `provider/account_id/endpoint` 等非 Secret 元数据，不得缓存长期 Bearer。每次 `_build_headers()` 或发起上游尝试前必须调用 `resolver.credential(codebuddy, account_id, chat)`。动态凭据轮换只更新 Vault/purpose 状态，不要求重建成员快照。

错误处理：

| 情况 | 行为 |
| --- | --- |
| 2xx/SSE 完成 | 标记该账号 chat 成功 |
| 401/403 | 有 refresh 时单飞 refresh 后重试一次；仍失败则 `needs_reauth` 并剔除 chat |
| 429 | 账号级短冷却，尊重有上限的 `Retry-After` |
| 5xx/连接错误 | 使用现有失败重试和冷却，不改其他账号状态 |
| static `ck_` 失败 | 只标记 static slot，不删除环境变量 |

### 8.4 Qoder chat

- 一个 `chat.pat` 对应一个 `QoderProvider` 和独立 `QoderSession`。
- session 建立继续使用当前 jobToken/COSY 流程。
- session 默认只存在进程内；不保存完整 COSY Authorization。
- 401 或明确 session 失效时，销毁该账号 session、重建一次并重试。
- Qoder check-in access refresh 绝不覆盖 `chat.pat` 或 COSY session。

### 8.5 动态热更新与 Provider 生命周期

```text
registry transaction commit
        |
        v
build candidate slot handles
        |
        v
DynamicProviderPool.update_slots(new_snapshot)
        |
        v
removed handles -> retiring -> close when in_flight == 0
```

每个 slot 使用内部 `SlotHandle`：

```text
SlotHandle
  key: (provider, account_id)
  provider: AccountBackedProvider
  state: active | retiring
  in_flight: integer
  generation: integer
```

- 选择账号时在 pool lock 内从 active snapshot 取得 handle 并递增 `in_flight`，返回 request lease；所有 complete/stream/cancel 路径都在 `finally` 释放 lease。
- `update_slots()` 先构造 candidate handles，再在短锁内原子替换 active snapshot。移除或 endpoint 变化的旧 handle 标记 `retiring`，立即拒绝新 lease，但不关闭 transport。
- retiring handle 在 `in_flight == 0` 时由 release 路径安排异步关闭；没有固定“延迟 N 秒”，也不能在 300 秒 Qoder 流仍运行时强制关闭。
- 同一稳定 key 的 credential refresh 不替换 handle；Provider 每次请求经 resolver 取得新版本。只有账号成员、endpoint 或需要重建的 session 配置变化才产生新 generation。
- health/cooldown 以稳定 key 存储；snapshot 重排不会把旧失败状态套到另一个账号。删除账号后可清理 active health，但旧 handle 的 drain 状态按对象/generation 保留到关闭。
- request outcome 同时携带 lease generation；若旧 retiring generation 在新 generation 启用后才返回失败，该失败只记审计，不得冷却同 key 的新 active handle。
- pool shutdown 先停止发放 lease，再等待 in-flight 归零。超过配置的 drain timeout 时记录未完成 key 并交给进程 shutdown，不得主动关闭仍被 lease 使用的 client。

这一机制是最小的引用计数，不扩展为跨进程 lease、分布式 drain 或请求持久化。

### 8.6 流式提交点与重试边界

非流式请求在没有响应返回客户端前，可以按预算 refresh 或切换其他稳定 key。流式请求必须维护 `committed` 状态：

```text
acquire slot lease
iterate upstream stream
before first non-empty downstream bytes:
  failure -> release -> refresh/failover allowed
yield first non-empty bytes:
  committed = true
after committed:
  failure -> mark this slot failed -> terminate this stream
             no refresh replay, no second account, no synthetic successful [DONE]
finally release lease
```

MVP 采用保守、可实现的提交点：`DynamicProviderPool` 第一次向上层 wrapper yield 任意非空 bytes 时即视为 committed，包括 role、content、thinking 或 tool-call preamble。这个时间点不晚于 ASGI 向客户端发送首 chunk；即使上层转换器短暂缓冲，也只会少一次 failover 机会，不会拼接两次生成。OpenAI 和 Anthropic 转换/日志 wrapper 都只能传播当前流终止，不能捕获异常后重新调用 pool，也不需要实现网络 ACK 回调。

必须有回归测试证明：首 chunk 前失败会尝试第二个账号；首 chunk 后失败不会调用第二个账号；客户端取消会释放 in-flight；partial tool call 不会拼接另一账号输出。

## 9. CodeBuddy/WorkBuddy 签到

### 9.1 认证模式

```text
checkin.mode = inherit_chat | bearer | cookie | bearer_cookie
```

- `inherit_chat`：只在 CB-CHECKIN-01 证明 chat Bearer 可签到后启用。
- `bearer`：明确使用签到 access token。
- `cookie`：使用完整 Cookie，不自动猜测或拼接。
- `bearer_cookie`：只有 cURL 证明二者必须同时存在时使用。

静态 `ck_` 对应的 `account_purposes(checkin).enabled` 默认是 `false`，实测成功后显式启用。OAuth 登录成功也不自动宣称签到可用。

### 9.2 请求构造

`WorkBuddyClient` 只接收单账号 request profile：

```text
base_url: https://www.workbuddy.cn
status_path: /billing/meter/checkin-status
status_method: <unset until CB-CHECKIN-01>
claim_path: /billing/meter/daily-checkin
claim_method: POST
body: {}
auth_mode: bearer | cookie | bearer_cookie
optional_headers: X-User-Id, X-Enterprise-Id, X-Tenant-Id, X-Domain
```

使用 `httpx.AsyncClient`、明确 timeout、禁止跨账号共享 CookieJar。base URL 取 provider allowlist，path 从应用配置加载，不能由单次请求任意传入。

### 9.3 幂等流程

```text
1. 获取账号 checkin credential
2. 只有 CB-CHECKIN-01 同时确认 method 和语义后才调 checkin-status；否则跳过 preflight
3. 已签到 -> ALREADY_CHECKED_IN
4. 未签到/可领取 -> 调 daily-checkin
5. 2xx -> CLAIMED
6. HTTP 400 + code=10001 -> ALREADY_CHECKED_IN
7. 401/403 -> purpose refresh 一次；失败 -> NEEDS_REAUTH
8. 429/5xx/网络超时 -> 有限重试
9. 其他 4xx/解析失败 -> FAILED，不重试轰炸
```

`requestId` 可以保存；Authorization、Cookie 和完整响应 body 不得保存。

### 9.4 结果分类

```text
CLAIMED
ALREADY_CHECKED_IN
AUTH_FAILED
NEEDS_REAUTH
RATE_LIMITED
TRANSIENT_ERROR
FAILED
SKIPPED
```

`ALREADY_CHECKED_IN` 是业务成功，不影响 Proxy 状态，也不进入重试队列。

## 10. Qoder 签到

### 10.1 默认双凭证

```json
{
  "provider": "qoder",
  "account_id": "qd-main",
  "chat": {
    "pat": "<encrypted>"
  },
  "checkin": {
    "access_token": "<encrypted>",
    "refresh_token": "<encrypted>",
    "expires_at": 1750007200,
    "user_agent": "QoderWork"
  }
}
```

现有 `QODER_TOKEN=pt_a,pt_b` 只生成 transient env chat slots，UI 显示 `checkin=needs_promotion`；只有 promotion 产生持久随机账号后，才能导入 access/refresh 并显示 `checkin=needs_import`。导入 access/refresh 后补齐同一个动态 `account_id`，不创建重复账号。

### 10.2 refresh 规则

```text
now < expires_at - skew -> 使用缓存 access
now >= expires_at - skew -> purpose lock 下调 deviceToken/refresh
响应有 device_token 或 token -> 更新 access
响应有新 refresh_token -> 原子替换旧 refresh
响应没有新 refresh_token -> 保留旧 refresh
refresh 失败且旧 access 未明确过期 -> 允许一次原 token 请求
原 token 也 401 或明确过期 -> NEEDS_REAUTH
```

不能照搬参考实现“refresh 失败后一直使用旧 token”的行为；已过期凭据应尽快进入 `needs_reauth`，避免每天重复无效请求。

### 10.3 首次导入

Qoder check-in 的 Mac Mini 标准首次开通路径固定为“已登录 Windows 一次性导出 -> HTTPS 导入 Mac Mini -> 服务端验证 -> 后续 headless refresh”：

1. 在已安装并登录 QoderWork CN 的 Windows 用户会话中运行 Phase 4 交付的一次性 exporter。第一版 exporter 以参考工程 `022c1d4` 的 Windows DPAPI + AES-GCM 解密链为协议依据，但单独审计、单独构建，不移植 Tauri UI 和账号切换逻辑。
2. exporter 只产生 `version/access_token/refresh_token/expires_at` 和可选脱敏 identity，不导出完整 profile、Cookie、COSY Authorization、PAT 或设备目录。它不得把 Secret 写日志或 stdout；临时文件使用当前用户独占 ACL。
3. 用户在 `https://<tailscale-host>/admin` 选择既有动态 Qoder `account_id`；若当前只有 `qd-env-N`，先执行 promotion，再上传最小 JSON，或把字段粘贴到一次性表单。不得把 API Key 或 Token 放进命令行参数、URL 或浏览器持久存储。
4. `POST /api/admin/auth/qoder/checkin` 先做 schema/body-size 校验；access 即将过期时先 refresh，然后调用 Qoder status。status 成功是提交硬门槛；若 chat 与 check-in 两侧都有稳定 identity，必须匹配。chat 侧没有可比 identity 时，由用户显式选择 `account_id` 完成绑定，并在 AccountView 记录 `identity_match=unavailable`，不能伪报为已匹配。
5. 验证通过后才在单个事务中加密保存 credential、更新 `account_purposes(checkin)` 并删除服务端临时值；失败不得覆盖旧 credential。
6. 用户删除 Windows 和传输端的临时文件；Mac Mini 后续只通过 refresh + status/claim 运行，不要求 QoderWork GUI 或 exporter 常驻。

最小导入格式：

```json
{
  "version": 1,
  "access_token": "<secret>",
  "refresh_token": "<secret>",
  "expires_at": "2026-07-23T00:00:00Z"
}
```

若没有可执行的 Windows 解密环境，受保护管理页手动录入同一最小 payload 是兜底路径。参考 switcher 的非 Windows 解密是 stub，因此第一版明确不承诺 macOS 自动读取 `auth-v2.dat`；无法取得一次性 check-in 凭据时，该账号保持 `checkin=needs_import`，chat PAT 仍正常工作。这是 Mac Mini 无人值守签到的运维前提，不是可忽略的可选步骤。

### 10.4 claim 流程

```text
for account in enabled qoder checkin accounts:
  access = resolver.get(qoder, account_id, checkin)
  GET status
  if CLAIMED_TODAY: record ALREADY_CHECKED_IN
  else POST claim with {}
  on 401: refresh once and retry same purpose
  on refresh failure: record NEEDS_REAUTH, continue next account
```

## 11. 统一调度器

### 11.1 配置

```ini
CHECKIN_ENABLED=true
CHECKIN_AT=00:10
CHECKIN_TIMEZONE=Asia/Shanghai
CHECKIN_CATCH_UP=true
CHECKIN_CATCH_UP_WINDOW_HOURS=6
CHECKIN_JITTER_MIN_SECONDS=3
CHECKIN_JITTER_MAX_SECONDS=10
CHECKIN_REQUEST_TIMEOUT_SECONDS=15
CHECKIN_RETRY_LIMIT=2
METRICS_ENABLED=true
METRICS_REFRESH_INTERVAL_SECONDS=900
USAGE_ROLLUP_INTERVAL_SECONDS=60
USAGE_DETAIL_RETENTION_DAYS=90
USAGE_ROLLUP_RETENTION_MONTHS=24
QB2API_WORKER_PORT=10001
PROVIDER_DRAIN_TIMEOUT_SECONDS=330
QB2API_WORKER_SHUTDOWN_TIMEOUT_SECONDS=15
```

使用 `zoneinfo.ZoneInfo`，不增加 cron 解析依赖。

### 11.2 生命周期

```text
Control Plane lifespan enter
  validate config and run SQLite migrations
  open async Repository and CredentialVault
  load runtime settings and AccountRegistry
  create CheckinScheduler, MetricsScheduler, UsageRollupScheduler
  restore ServiceSupervisor state and reconcile orphan Worker safely
  yield
Control Plane lifespan exit
  stop schedulers and await active operations
  drain/stop Worker through Supervisor
  close upstream check-in clients and Repository

Worker lifespan enter
  validate internal token and protocol version
  load read-only runtime snapshot and model catalog
  create stable DynamicProviderPool(0..N)
  expose /health/live, /internal/health/ready and proxy routes
  yield
Worker lifespan exit
  reject new requests and drain active streams
  flush bounded telemetry queue
  close Provider sessions and exit
```

重启时，scheduler 查询 `checkin_daily_state`，当天已达到 `CLAIMED` 或 `ALREADY_CHECKED_IN` 的账号不重复执行；错过计划但仍在 catch-up window 内，则小 jitter 后补跑未完成账号。超过窗口不自动补跑，可从 UI 手动执行。

### 11.3 批次与重入

- 定时和手动运行共享一把 `asyncio.Lock`。
- 已有批次运行时，手动 API 返回 `409 checkin_run_in_progress`，不排队、不并发。
- 每个账号独立 `try/except`，结果落库后再进入下一账号。
- 默认 CodeBuddy/WorkBuddy 后 Qoder，同一批次不交叉并发。

### 11.4 重试

只重试网络错误、超时、429、502/503/504。使用指数 backoff + jitter，并限制 `Retry-After`。

不重试：WorkBuddy `400/code=10001`、除一次 refresh retry 外的 401/403、其他业务 4xx、4xx 无法解析 JSON。

### 11.5 MetricsScheduler 与积分/Token 监控

`MetricsScheduler` 只读取已验证且 purpose 启用的凭据，按 provider 能力矩阵调用 token status、积分、quota 和签到摘要接口。每个账号每个 metric kind 单飞，默认 15 分钟刷新，失败按指数退避并保留上一次快照；凭据过期或返回 401 时只更新对应 purpose 的 `needs_reauth`/metric `unavailable`，不能禁用 chat。

监控结果写入 `account_metric_snapshots`，由 `/api/admin/accounts`、账号详情和 Overview 聚合。快照带 `observed_at/expires_at/status`，UI 超时显示 stale；接口没有可验证积分时返回 `status=unknown`，禁止将缺失值显示为 0。刷新动作返回 operation id，不能同步阻塞管理请求。

### 11.6 UsageRollupScheduler 与 Worker telemetry

Worker 为每个请求生成最小 `request_event`：在请求开始时分配 request id，结束/取消/异常时写状态、账号、模型、协议、HTTP 状态、耗时和 token usage。首个 chunk 前的 failover 只保留最终成功账号；已提交流的终止事件仍保留原账号和 `stream_committed=true`。

Control Plane 通过 loopback internal RPC 或有界异步队列接收事件。`UsageRollupScheduler` 每分钟按 provider/account/model 聚合到分钟桶，随后生成日/月 rollup；事件写入和聚合均采用短事务。队列、数据库或 Worker 暂时不可用时，Proxy 继续服务并在 health/metrics 中报告丢弃数量。

### 11.7 runtime settings 应用

设置更新先写 `runtime_settings`，再由 `SettingsApplier` 按 apply mode 执行：scheduler 设置采用构建新实例后原子替换；Worker 配置采用 snapshot reload，协议不兼容或 reload 失败才由 Supervisor restart；Control Plane 级设置返回 `restart_required=true`。应用结果写 `audit_events` 和 `service_runtime`，UI 使用 operation id 查询最终状态。

## 12. 管理 API 契约

除 `POST /api/admin/session` bootstrap 外，所有 `/api/admin/*` 需 Admin Key Bearer 或管理会话；响应不返回原始凭据。UI shell/静态资源遵循 7.1 的独立 public 规则。Proxy Key 对这些路由必须返回 403，而不是尝试降级认证。

### 12.1 会话

```text
POST /api/admin/session
  body: { "admin_key": "..." }
  response: { "status": "ok", "csrf_token": "..." }
POST /api/admin/session/logout
POST /api/admin/session/logout-all
GET  /api/admin/session
```

session bootstrap 自行执行 constant-time Admin Key 校验和登录限流。任何 Key 都不记录日志；UI 使用 HttpOnly cookie + CSRF，CLI 管理操作使用 Admin Key Authorization header。session cookie 不能认证 `/v1/*`、`/api/config` 或其他非 `/api/admin` API。

### 12.2 账号

```text
GET    /api/admin/accounts
GET    /api/admin/accounts/{provider}/{account_id}
PATCH  /api/admin/accounts/{provider}/{account_id}
DELETE /api/admin/accounts/{provider}/{account_id}
POST   /api/admin/accounts/{provider}/{account_id}/refresh
POST   /api/admin/accounts/{provider}/{account_id}/probe
POST   /api/admin/accounts/{provider}/{account_id}/promote
```

`PATCH` 只修改 label、账号全局 enabled，以及 `purposes.chat/checkin` 的 enabled、认证模式等白名单元数据，不直接写汇总状态，不接受任意 URL。`DELETE` 对 env static 账号返回明确错误。`promote` 只接受 env slot，按 5.1 生成新的随机动态账号并返回新 AccountView；不能让调用方指定目标 `account_id` 或取得 Secret。

### 12.3 登录和导入

```text
POST /api/admin/auth/codebuddy/start
POST /api/admin/auth/codebuddy/poll
POST /api/admin/auth/codebuddy/manual
POST /api/admin/auth/qoder/chat
POST /api/admin/auth/qoder/checkin
POST /api/admin/auth/workbuddy/checkin
```

导入成功只返回 AccountView、purpose 状态、capabilities 和过期时间，不返回 Secret。Qoder check-in 导入必须通过 10.3 的服务端 status 验证后才提交事务；失败不覆盖现有可用 credential。

OAuth poll body：

```json
{
  "flow_id": "opaque-one-time-id"
}
```

### 12.4 签到

```text
GET  /api/admin/checkin/status
POST /api/admin/checkin/run
GET  /api/admin/checkin/runs/{run_id}
```

手动运行 body：

```json
{
  "targets": [
    {"provider": "codebuddy", "account_id": "cb-alice"},
    {"provider": "qoder", "account_id": "qd-main"}
  ]
}
```

省略 `targets` 表示所有启用账号；显式目标使用完整复合主键，避免不同 provider 的同名账号产生歧义。请求不接受 Token、Cookie、URL 或任意上游请求头。

### 12.5 服务生命周期

```text
GET  /api/admin/service
POST /api/admin/service/start
POST /api/admin/service/stop
POST /api/admin/service/restart
POST /api/admin/service/reload
GET  /api/admin/service/operations/{operation_id}
GET  /api/admin/service/events
```

生命周期接口只控制 Proxy Worker。请求由 Supervisor 校验当前 owner、desired state 和幂等 operation key；响应先返回 operation id，UI 轮询到 `succeeded|failed|cancelled`。`stop` 返回 draining/in-flight/grace deadline；控制面自身没有停止 API。未经验证的 PID、启动时间、owner 或内部 token 不得触发 signal。

`/api/control/*` 保留给 Control Plane 与 Worker 的 loopback 内部契约：

```text
POST /api/control/worker/handshake
GET  /api/control/worker/runtime-snapshot
POST /api/control/worker/telemetry
GET  /api/control/worker/health
```

这些路由只接受 loopback、内部 token 和 protocol version，不能由浏览器 session 或 Proxy Key 调用。

### 12.6 设置、模型、用量和监控

```text
GET   /api/admin/settings
PATCH /api/admin/settings
GET   /api/admin/settings/schema

GET   /api/admin/models
PATCH /api/admin/models/{provider}/{model_id}
POST  /api/admin/models/refresh
POST  /api/admin/models/{provider}/{model_id}/probe

GET   /api/admin/usage/summary
GET   /api/admin/usage/timeseries
GET   /api/admin/usage/events
GET   /api/admin/usage/events/{event_id}
GET   /api/admin/usage/export

GET   /api/admin/metrics/accounts
GET   /api/admin/metrics/accounts/{provider}/{account_id}
POST  /api/admin/metrics/refresh
```

所有查询都支持明确的时间范围、provider/account/model 过滤和分页上限。Usage event detail 仅返回 request id、模型、账号、状态、耗时、token、错误码和 `stream_committed`，不返回 prompt、completion、Authorization、Cookie 或上游完整响应。导出只允许脱敏 rollup/event 字段，并记录审计。

### 12.7 凭据、备份和审计

```text
GET   /api/admin/credentials
POST  /api/admin/credentials/{provider}/{account_id}/{purpose}/rotate
POST  /api/admin/credentials/{provider}/{account_id}/{purpose}/revoke

GET   /api/admin/audit
GET   /api/admin/backup
POST  /api/admin/backup
GET   /api/admin/backup/{backup_id}
POST  /api/admin/backup/{backup_id}/restore
```

凭据接口只返回 mode、version、过期、refresh 能力和验证状态；rotate/revoke 需要 CSRF、二次确认和审计。恢复备份默认先做 schema/key/完整性校验和 dry-run，生成 operation id，不能直接覆盖当前数据库。

## 13. 配置

### 13.1 服务和存储

```ini
QB2API_CONTROL_HOST=127.0.0.1
QB2API_CONTROL_PORT=9999
QB2API_WORKER_HOST=127.0.0.1
QB2API_WORKER_PORT=10001
QB2API_PROXY_API_KEY=<optional-for-legacy-open-proxy>
QB2API_ADMIN_KEY=<required-for-admin-ui-dynamic-checkin>
# QB2API_API_KEY=<deprecated-proxy-alias>
QB2API_DATA_DIR=./data
QB2API_CREDENTIAL_KEY=<fernet-key-required-for-persistent-secrets>
QB2API_ADMIN_UI_ENABLED=true
QB2API_ADMIN_UI_PATH=/admin
QB2API_ADMIN_COOKIE_SECURE=auto # false=显式允许受信 Tailscale/LAN HTTP
QB2API_ADMIN_SESSION_TTL_HOURS=12
QB2API_ADMIN_SESSION_IDLE_MINUTES=60
QB2API_TRUSTED_PROXY_HEADERS=false
QB2API_TRUSTED_PROXY_NETWORKS=<optional-CIDR-list>
QB2API_WORKER_AUTOSTART=true
QB2API_WORKER_START_TIMEOUT_SECONDS=30
QB2API_WORKER_HEALTH_INTERVAL_SECONDS=5
PROVIDER_DRAIN_TIMEOUT_SECONDS=330
QB2API_WORKER_SHUTDOWN_TIMEOUT_SECONDS=15
QB2API_WORKER_INTERNAL_TOKEN=<generated-or-secret-file>
QB2API_RUNTIME_SETTINGS_ENABLED=true
```

Control Plane 默认只绑定 loopback；优先由 Tailscale Serve/Caddy 提供远程 HTTPS ingress。无法部署 HTTPS 时，可把 Control Plane 显式绑定到 Tailscale IP（优先于 `0.0.0.0`），配置 `QB2API_ADMIN_COOKIE_SECURE=false` 并用 tailnet ACL/主机防火墙限制来源。为兼容旧 proxy，可将 Worker 通过显式反向代理暴露到原入口；Control Plane 同时把 `/v1/*` 从管理端口转发到 loopback Worker 作为统一入口——转发只发生在进程边界，两者监听地址不共享，管理端口不直接处理模型请求。启用管理 UI、动态凭据或签到时无 `QB2API_ADMIN_KEY`/主密钥拒绝启动；非 loopback 管理必须满足 7.2 的 cookie 契约。Proxy Key 可以为空以兼容旧的开放 proxy，但远程 proxy 应显式配置 `QB2API_PROXY_API_KEY`。旧 `QB2API_API_KEY` 只映射 Proxy 权限；配置了两个新 Key 且值相同必须拒绝启动。

`QB2API_WORKER_INTERNAL_TOKEN` 只给 Supervisor/Worker 的内部握手使用，不能作为客户端 API key 或 Admin Key。若未显式配置，首次启动生成 256-bit 随机 token 写入 `QB2API_DATA_DIR/worker.internal`（0600），并在 Worker restart 时递增 auth version；Control Plane 不把它返回 UI。

可写运行设置存储在 `runtime_settings`，环境变量只在没有 runtime override 时作为 source=env。设置修改必须通过 12.6 的版本化 PATCH 和 7.4 的 apply mode；禁止 UI 直接改 `.env`。

### 13.2 Provider

```ini
CODEBUDDY_TOKEN=ck_a,ck_b
CODEBUDDY_ENDPOINT=https://copilot.tencent.com
CODEBUDDY_OAUTH_ENABLED=true
CODEBUDDY_OAUTH_TIMEOUT_SECONDS=20
CODEBUDDY_OAUTH_REFRESH_SKEW_SECONDS=120

QODER_TOKEN=pt_a,pt_b
QODER_TIMEOUT=300
PROVIDER_DRAIN_TIMEOUT_SECONDS=330
```

### 13.3 签到端点

```ini
CODEBUDDY_CHECKIN_ENABLED=true
CODEBUDDY_CHECKIN_BASE=https://www.workbuddy.cn
CODEBUDDY_CHECKIN_STATUS_PATH=/billing/meter/checkin-status
CODEBUDDY_CHECKIN_STATUS_METHOD=
CODEBUDDY_CHECKIN_CLAIM_PATH=/billing/meter/daily-checkin
CODEBUDDY_CHECKIN_CLAIM_METHOD=POST

QODER_CHECKIN_ENABLED=true
QODER_CHECKIN_BASE=https://openapi.qoder.com.cn
QODER_CHECKIN_STATUS_PATH=/sash/api/v1/me/daily-check-in/status
QODER_CHECKIN_CLAIM_PATH=/sash/api/v1/me/daily-check-in/claim
QODER_CHECKIN_REFRESH_PATH=/api/v1/deviceToken/refresh
```

空的 `CODEBUDDY_CHECKIN_STATUS_METHOD` 表示禁用 status preflight，不允许猜测 POST。WorkBuddy status method/auth 在 CB-CHECKIN-01 后固定；Qoder PAT 合并方向在 QD-CHECKIN-01 后决定。未验证能力使用 `verification_status=unverified`，不自动调度。

## 14. 模块和文件落点

```text
src/qb2api/
  control/
    app.py                # persistent Control Plane factory/lifespan
    supervisor.py         # Worker process state machine, PID/owner/token checks
    operations.py         # async operation records and polling
    settings.py           # runtime settings schema, version and apply modes
    telemetry.py          # bounded Worker event intake and backpressure
  accounts/
    models.py             # AccountRecord, AccountView, Capability, Status, VerificationStatus
    repository.py         # async DB boundary, WAL, transaction/version API
    schema.py             # migration/version registry and table definitions
    vault.py              # Fernet encrypt/decrypt and key validation
    registry.py           # merge env + DB, enable/disable, snapshots
    resolver.py           # purpose credential, skew refresh, single-flight
  admin/
    auth.py               # Proxy/Admin key policy and HttpOnly session/CSRF
    router.py             # accounts/auth/checkin/settings/models/usage routes
    service_router.py      # worker lifecycle routes and operation polling
    audit.py               # redacted audit writer/query
    backup.py              # dry-run backup/restore workflow
  auth/
    codebuddy_oauth.py    # state/token/poll/refresh upstream client
    flows.py              # TTL, one-time state, redacted result
  checkin/
    models.py             # Outcome, Run and Attempt DTOs
    base.py               # client protocol and classifier
    codebuddy.py          # WorkBuddy status and claim
    qoder.py              # Qoder status, claim and refresh
    service.py            # account and batch orchestration
    scheduler.py          # zoneinfo, catch-up, lifecycle
    metrics.py             # token/points/quota snapshot refresh
    usage.py               # request event rollup and retention
  providers/
    codebuddy.py          # account-backed chat provider
    qoder.py              # account-backed PAT/COSY provider
    lb.py                 # DynamicProviderPool(0..N), SlotHandle lease, stream commit, drain
  worker/
    app.py                # independent Proxy Worker factory/lifespan
    runtime.py            # read-only snapshot, provider/model assembly
    internal_router.py    # handshake/health/telemetry, loopback only
    proxy_router.py       # OpenAI/Anthropic compatibility endpoints
    model_catalog.py      # model discovery and capability snapshot
    request_events.py     # non-blocking request telemetry
  web/
    dist/                 # Vite production output, served by Control Plane
  config.py               # shared environment binding and validation
  app.py                  # compatibility entrypoint selecting control/worker mode

frontend/
  package.json            # Vue/Vite/Pinia/Query/ECharts/Lucide toolchain
  src/
    app/                   # router, query client, global error/loading state
    layouts/               # AdminShell, Sidebar, Header, ServiceRail
    pages/                 # Overview, Service, Accounts, Models, Usage, Checkin, Settings, Audit
    components/            # DataTable, Charts, Drawers, ConfirmDialog, StatusBadge, EmptyState
    stores/                # auth, service, settings, ui preferences
    api/                   # typed admin/control clients and mutation invalidation
    styles/                # design tokens, responsive layout, accessibility
  tests/                   # component and route contract tests

pyproject.toml            # async SQLite/runtime dependencies and scripts
package-lock.json         # committed frontend lockfile

tools/
  qoder-checkin-exporter/ # Windows one-shot exporter; 不随服务常驻
deploy/
  launchd/                 # Control Plane plist; Worker is child-owned by Supervisor
  systemd/                 # optional Linux development service
```

保持现有 `Provider` 抽象和 OpenAI/Anthropic 转换层的协议行为不变，但将执行入口迁移到 `worker/proxy_router.py`；Control Plane 的 `app.py` 只装配管理域和 Supervisor。共享 DTO 放在小型 `contracts.py`，禁止 Control Plane 直接 import Worker 的可变 Provider 状态。前端构建产物必须通过脚本复制到 `src/qb2api/web/dist`，源码不内嵌 Secret，也不使用 `innerHTML` 渲染不可信内容。

## 15. 迁移策略

### 15.1 旧环境变量兼容

首次启动：

1. 读取 `CODEBUDDY_TOKEN` 和 `QODER_TOKEN`。
2. 仅在内存生成 `cb-env-N` / `qd-env-N` transient chat slots；Token 轮换不改同一进程内槽位 ID，但列表重排仍可能改变槽位对应账号。
3. env slots 不写入 `accounts/account_purposes`，不承载持久 label、签到凭据、历史身份或审计记录；长期使用必须通过 Admin promotion 创建随机动态账号。
4. 合并 DB 动态账号时，按同 provider + purpose 在内存中 constant-time 比较解密后的 credential；发现相同 Secret 时动态账号优先，env slot 增加只读视图标记 `shadowed=true` 并排除出 pool，避免双倍流量。`shadowed` 不是 purpose 状态，原始环境变量不被改写。
5. env slot 被移除时随下一次 registry rebuild 消失，不保留可误绑定到新下标的持久行；已 promotion 的动态账号不受 env 列表重排影响。
6. keyed HMAC Secret fingerprint 和上游 identity hash 只用于内部去重/匹配，不作为账号 ID；API、UI 和普通日志不显示。
7. promotion 后动态账号使用 Vault 中的独立副本；同值 env slot 被 shadow。以后轮换应通过动态账号 API 完成，并从 env 移除旧副本；直接改 env 下标视为新的 transient credential，不会静默覆盖动态账号。
8. 保持模型列表、provider 名称和 `/v1`、`/v1/messages` 行为不变；无 slot 的稳定 pool 返回明确 unavailable，而不是启动失败。
9. 账号 pool 完成装配后才启动 scheduler，避免半初始化触发签到。

### 15.2 `/api/config` 兼容与弃用

- 现有 `PATCH /api/config` 的 `codebuddy_token(s)`、`qoder_token(s)` 至少保留账号 API 发布后的一个兼容周期，仍只写 `.env`，并返回 `restart_required=true`；该路由只接受 Admin Key Bearer。
- 旧字段 `api_key` 仍可作为 deprecated alias 写 `QB2API_PROXY_API_KEY`，但不能写 Admin Key；Admin Key 轮换必须走受控部署/secret 管理并撤销全部 session。
- 响应新增 `deprecated_fields` 和账号 API 迁移提示；不能立即改成 `410`，移除前必须单独发布迁移公告。
- 新的 OAuth、manual/import、promotion 和账号编辑 API 永不回写 `.env`。动态账号写 SQLite/Vault，提交后更新稳定 pool。
- `GET /api/config` 及 PATCH 响应继续只返回掩码；不得为了去重回显 Secret 或指纹。
- env 与 DB 同时存在时遵循 15.1 的动态账号优先规则；删除动态重复账号后，下一次 pool rebuild 恢复 env slot。

### 15.3 动态账号迁移

- `workbuddy_api/.codebuddy_creds/token.json` 不自动扫描或导入；跨项目自动读取 Secret 风险不可接受。
- 被合并的旧设计文档 JSON 只是方案，不是已投产格式；只有明确的 UI/API import 才进入新仓库。
- Qoder 只有 PAT 时只开启 transient env chat；check-in 为 `needs_promotion`。promotion 后没有 access/refresh 时为 `needs_import`。
- 删除数据库账号不删除环境变量；重启后 static slot 会恢复。

### 15.4 模型路由兼容

继续支持：

```text
codebuddy/<model>
qoder/<model>
```

模型定义文件仍是模型能力来源。账号变化只改变 provider 是否可用，不改变模型 ID。

### 15.5 单进程到 Supervisor/Worker 迁移

迁移必须先备份 `.env`、SQLite 和凭据主密钥，再执行一次 dry-run。Control Plane 先启动并完成 schema migration，导入 env static slots 和动态账号；只有 registry/model snapshot 校验成功后才启动 Worker。

```text
旧单进程
  ├─ 读取 env
  ├─ 创建 Provider/LB
  └─ 暴露 proxy + admin

迁移启动
  ├─ Control Plane 绑定新管理端口
  ├─ Supervisor 检查旧端口/owner，不盲杀
  ├─ Worker 在 loopback 新端口健康握手
  └─ 反向代理切换到 Worker
```

- 旧 `QB2API_HOST/QB2API_PORT` 在一个兼容周期内映射为 Worker ingress 配置；Control Plane 使用独立端口，避免客户端与管理台共享生命周期。
- 若发现旧实例仍占用目标 Worker 端口，迁移命令只报告冲突并要求人工停止，不能按端口 kill。
- Supervisor 启动后写入 owner instance id 和 process start time；重启只操作匹配同一 owner 的 Worker。
- 迁移失败时保留旧 `.env` 和数据库，不覆盖凭据；可以停止新 Worker 并回到旧单进程入口。回滚不删除新表，下一次启动继续从 migration checkpoint 运行。
- 迁移完成后旧 proxy/admin 入口只保留 410/redirect 兼容响应，具体时间和客户端切换公告作为单独运维变更，不在代码中静默移除。

## 16. 验证与测试策略

### 16.1 测试分层

单元测试不访问真实上游，也不把真实 Token 放进 fixture。

1. **Vault/Repository**：加密读写、WAL/foreign key/busy timeout、短事务、并发写、optimistic credential version、错误 key、随机账号 ID、purpose 状态/验证状态隔离、schema 迁移。
2. **OAuth flow**：`11217`、success、过期 state、重复 poll、异常 JSON、响应脱敏。
3. **Resolver**：skew、purpose single-flight、credential cache/version 失效、refresh 轮换冲突、失败状态转移；命中缓存时不查询 SQLite。
4. **Proxy pool**：0/1/N snapshot、首账号加入/最后账号删除、动态模型可见性、稳定 key health、round-robin、401 refresh、逐请求 resolver、retiring lease、取消释放、热更新。
5. **Streaming**：首个下游 chunk 前允许 failover，首 chunk 后禁止跨账号重试；partial content/tool call 后第二账号调用次数必须为 0。
6. **Qoder session**：每 PAT 独立、session 重建、COSY Header 不泄漏。
7. **WorkBuddy client**：status method 为空时不发 preflight、Bearer/Cookie 账号隔离，`400/code=10001` 无重试。
8. **Qoder check-in**：promotion 门禁、最小导入校验、身份不匹配不覆盖、status/claim/refresh 分类、refresh token 轮换写回。
9. **Scheduler**：时区、跨日、catch-up、批次锁、失败继续、取消和关闭。
10. **API/UI**：Proxy/Admin Key 权限矩阵、bootstrap、登录限流、session/CSRF、Secure cookie、promotion、runtime settings version/apply、无 Secret 响应、409。
11. **配置兼容**：旧 Key 只映射 Proxy；`/api/config` 只认 Admin Key；旧 token PATCH 仍写 env 且报告 deprecated/restart；动态账号不写 env；重复 Secret 不产生双实例。
12. **回归**：原有 OpenAI、Anthropic、工具调用、模型路由全部通过。

### 16.2 定向验证命令

实现阶段先安装仓库已声明的 dev extra，再执行：

```bash
pip install -e '.[dev]'
pytest -q tests/test_accounts.py tests/test_auth.py tests/test_checkin.py
pytest -q
ruff check src tests
git diff --check
```

`ruff` 已存在于 `pyproject.toml` 的 `dev` optional dependency 和 `[tool.ruff]` 配置中，因此它是确定的质量门禁，不使用“若已安装则跳过”的弱约束。

真实 smoke 只在明确授权账号上运行。输出记录 provider、account_id、HTTP 状态、业务码、requestId 和耗时，不记录请求头或响应原文。

### 16.3 外部 Spike 交付物

实现前产生脱敏 `spike-results.md` 或同等审计记录：

```text
CB-CHECKIN-01: URL、method、auth mode、header names、body shape、10001
QD-CHECKIN-01: pt_ claim、session claim、refresh、refresh rotation
AUTH-01: CodeBuddy expiresIn、refreshToken、refresh endpoint
```

该记录不放入 `docs/design`，避免 design 目录重新出现多个设计文件；它属于实现验证产物。

## 17. 分阶段实施顺序

### Phase 0：基线、Spike 和工具链

- 固化现有 `pytest`、配置兼容和 OpenAI/Anthropic proxy 行为。
- 完成 CB-CHECKIN-01、QD-CHECKIN-01、AUTH-01；未确认协议只保留 feature flag，不猜测 method/凭据。
- 固化 Proxy/Admin Key 权限矩阵、Worker 内部 token、0/1/N pool、partial stream 回归用例。
- 建立 Vue/Vite/TypeScript 构建和 Playwright smoke 基线，确定静态产物复制到 `src/qb2api/web/dist` 的脚本。

验收：原有测试保持通过；新前端可以构建、匿名 shell 不泄漏管理数据；Spike 结果可追溯。

### Phase 1：Control Plane + Worker + Supervisor 最小闭环

- 拆出 `control/app.py`、`worker/app.py` 和兼容入口；Worker 只 loopback 监听并实现 handshake/health/telemetry。
- 实现 `ServiceSupervisor` 状态机、operation id、PID/start-time/owner/token 校验、drain/kill 超时和 orphan reconcile。
- 将现有 OpenAI/Anthropic 路由迁移到 Worker，旧模型协议和 Proxy Key 行为保持不变；Control Plane 可在 Worker 停止时继续返回管理 shell/health。
- 建立 `aiosqlite` Repository/Vault/migration、runtime settings、service_runtime、proxy_api_keys 和 admin session 基础表。

验收：可从管理 API start/stop/restart Worker；错误 PID 不会被终止；Worker 停止不影响 Control Plane；旧 proxy 回归通过。

### Phase 2：账号、凭据和完整管理台骨架

- 完成稳定 account_id、purpose 状态、credential version cache、动态 0..N pool、env shadow/promotion 和无 Secret API。
- 实现 Admin Key/session/CSRF/限流与 `/admin` Vue shell；交付 Overview、Service、Accounts、Credentials、Settings 基础页面、路由守卫、DataTable、抽屉和错误/空/加载状态。
- 账号增删/启停/探测/刷新通过 API 更新 registry snapshot，再触发 Worker reload；退役 slot 做 lease drain。

验收：CodeBuddy/Qoder 静态和动态账号都能在 UI 管理；Proxy/Admin 权限严格隔离；账号 purpose 状态可独立显示；Playwright 完成登录、服务控制和账号详情 smoke。

### Phase 3：模型目录、用量与 Token/积分监控

- Worker 生成 model catalog 和 request events；Control Plane 实现 bounded telemetry intake、UsageRollupScheduler、保留策略和脱敏导出。
- 实现 MetricsScheduler、token status/points/quota/checkin snapshots、stale/unavailable 状态和按账号/provider 聚合。
- 交付 Models、Usage、Overview 图表和账号指标详情，加入分页、筛选、时间范围、刷新 operation 和审计。

验收：请求不会因 telemetry/rollup 暂时失败而失败；用量和 token 数与脱敏事件一致；积分未知不伪造为 0；前端图表和表格可在桌面/窄屏查看。

### Phase 4：CodeBuddy OAuth/WorkBuddy 签到纵向闭环

- 接入 OAuth/manual Bearer、CodeBuddy refresh/re-auth、WorkBuddy status/claim；`400/code=10001` 分类为 `ALREADY_CHECKED_IN` 且不重试。
- 完成 CheckinScheduler 的时区、catch-up、批次锁、jitter、手动指定账号和历史结果；签到失败不影响 chat。
- 交付 Check-in 页面、批次详情、失败重试和积分变化视图。

验收：至少两个 CodeBuddy 账号轮询；同一账号 `chat=active/checkin=needs_reauth` 可共存；至少一个真实授权账号完成定时或 catch-up 签到；partial stream 永不跨账号重放。

### Phase 5：Qoder 双凭证、模型和账号高级操作

- 将 Qoder PAT/COSY 纳入动态池，保证每 PAT session 独立、失效隔离和工具调用回归。
- 交付 Windows one-shot exporter、最小 JSON schema、HTTPS import、服务端 status/identity 验证、access/refresh 轮换和 `needs_import/needs_reauth` UI。
- 完成账号详情的凭据版本、Quota/points、Events、Check-in tabs，补齐批量操作和审计。

验收：Mac Mini 无 QoderWork GUI 可跨 access 过期连续签到；refresh 失败不影响 chat；PAT 不能未经 Spike 直接作为签到凭据。

### Phase 6：设置、备份、审计和部署硬化

- 完成 runtime settings schema、乐观版本、scheduler reschedule、Worker reload/restart 和 apply operation；UI 显示 source/apply mode/生效状态。
- 完成 backup/restore dry-run、Key 轮换、审计查询、Tailscale Serve/SSH loopback、launchd/systemd 模板和升级回滚文档。
- 做全量 pytest、ruff、frontend build/unit、Playwright desktop/mobile、fresh data smoke 和迁移回滚演练。

验收：所有用户要求可从前端完成；设置不出现“已保存但未生效”；服务控制、模型管理、token/积分监控、账号登录/管理、签到、审计和备份均有可追溯结果。

每个阶段都必须保留可运行纵向结果；阶段完成不等于可跳过下一阶段的安全/协议门禁。外部协议仍未验证时，UI 明确显示 unavailable/verification required，而不是渲染成功假象。

## 18. 部署拓扑与运行手册

```text
[Mac Mini]
  Control Plane: 127.0.0.1:9999 (persistent)
  Proxy Worker:  127.0.0.1:10001 (Supervisor-owned)
  SQLite + encrypted credentials: ./data/
  launchd/systemd starts Control Plane only
  Supervisor starts/stops Worker on demand

[管理入口（选择一种）]
  推荐：https://<tailscale-host> -> Tailscale Serve/Caddy -> 127.0.0.1:9999
  降级：http://<tailscale-ip>:9999 + QB2API_ADMIN_COOKIE_SECURE=false
        Control 只绑定 Tailscale IP，并用 tailnet ACL/主机防火墙限制来源
  只有来自受信 ingress 的请求才能接受 forwarded scheme

[Proxy ingress, recommended unified entry]
  CLI clients -> 127.0.0.1:9999/v1 (Control Plane forwards /v1/* to Worker)
  direct Worker 127.0.0.1:10001/v1 remains available as a compatible address
  upstream path must preserve Proxy API Key and never Admin cookie

[开发/维护电脑]
  浏览器访问推荐的 https://<tailscale-host>/admin
  或显式降级的 http://<tailscale-ip>:9999/admin（UI 显示风险警告）
  通过 auth_url 完成 CodeBuddy 登录
  按一次性 Windows exporter runbook 导入 Qoder check-in access/refresh
  必要时手动导入 WorkBuddy Cookie

[本地开发工具]
  OPENAI_BASE_URL=https://<proxy-host>/v1
  ANTHROPIC_BASE_URL=https://<proxy-host>
  API key = QB2API_PROXY_API_KEY
```

启动检查：

1. Proxy Key、Admin Key、凭据主密钥来自不同的安全配置项，两个 Key 不相同；旧 Key 只映射 Proxy。
2. data 目录权限正确。
3. SQLite schema 迁移成功。
4. static/dynamic 账号数量和 capability 符合预期。
5. Control Plane 只有一个 Checkin/Metrics/Usage scheduler 实例；下一次时间正确。
6. Worker owner/PID/start-time/internal token 与 `service_runtime` 一致，健康握手成功。
7. 远程入口优先为 HTTPS；若显式使用受信 HTTP，`QB2API_ADMIN_COOKIE_SECURE=false`、监听地址、tailnet ACL/主机防火墙和 UI 警告均已核对。`auto` 下远程 HTTP 必须登录失败。
8. 匿名只能取得 UI shell，所有账号数据 API 均返回未认证；admin session 不能调用 proxy API。
9. 停止 Worker 后管理台仍可登录、查看历史和修改设置；start/restart 后健康状态可恢复。

Qoder 首次开通：

1. Windows 上登录目标 QoderWork CN 账号并关闭可能并发改写 profile 的切换操作。
2. 运行版本和 commit 可追溯的 one-shot exporter，得到最小 JSON；确认输出没有 PAT、COSY session 或完整 profile。
3. 通过 HTTPS 管理页把 JSON 绑定到正确的持久 Qoder `account_id`；只有 env slot 时先 promotion。
4. 等待服务端 status 验证成功并显示 `checkin=active`；失败时保留旧 credential，按脱敏错误处理。
5. 删除临时 JSON；次日和 access 过期后分别核验 scheduler/refresh 记录，之后不再需要 Windows 或 QoderWork 常驻。

日常维护：

- 只通过 `/admin` 查看状态和重新授权，不手工编辑数据库。
- Worker 只能通过 `/admin/service` 控制；不要按端口使用 kill，异常进程先核对 owner/start-time/process-group。
- `needs_reauth` 先重新登录/导入，再恢复对应 purpose。
- 修改 endpoint/path 必须重做对应 Spike。
- 备份数据库时同时备份主密钥；缺少主密钥的备份不可恢复。
- env Token 列表只用于 transient chat；需要长期身份、签到或审计时先 promotion，不依赖 `cb-env-N/qd-env-N` 顺序。
- Worker telemetry 或 usage rollup 短暂不可用时先检查队列丢弃计数，不要重启 Control Plane；只有 Supervisor 状态机判定失败才执行 restart。

## 19. 风险与应对

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| WorkBuddy 路径/鉴权变化 | 签到失败 | 配置化 endpoint + cURL Spike + 分类测试 |
| Cookie 绑定设备/浏览器 | 手动 Cookie 失效 | Bearer-first；`needs_reauth`；不做无头浏览器主路径 |
| CodeBuddy refresh 不稳定 | OAuth 需重复登录 | refresh 失败转 `needs_reauth`，其他账号继续 |
| Qoder `pt_` 不能签到 | 需要双凭证 | 默认双凭证，不把 PAT 当 check-in token |
| Qoder refresh token 轮换 | 多日后失效 | 有新 refresh 时保存，无新值保留旧值 |
| macOS 不能解密 `auth-v2.dat` | 无法自动导出 | Windows one-shot exporter + HTTPS import；手动最小 payload 兜底，不依赖服务端解密 |
| Qoder 临时导出文件泄漏 | check-in 账号被接管 | 最小字段、用户独占 ACL、不写日志/stdout、导入后删除、服务端身份/status 验证 |
| purpose 状态相互覆盖 | 签到失败误停 chat | `account_purposes` 独立记录；汇总状态只读派生 |
| env Token 重排 | static 槽位关联漂移 | 槽位 ID 明示限制；长期账号迁为动态 ID；指纹不作主键 |
| env/DB 重复凭据 | 同一账号双倍流量 | 启动时内存 constant-time 去重，动态账号优先，env 标记 shadowed |
| Provider 缓存旧 Bearer | refresh 后持续 401 | 每次上游尝试经 resolver 取 versioned credential cache；pool membership 与凭据新鲜度分离 |
| 0/1/N pool 结构切换 | 首号加入或最后账号删除导致路由对象不存在 | lifespan 始终注册 `DynamicProviderPool`，无 slot 返回明确 503 |
| 热更新关闭旧 Provider | 中断流式请求或关闭错误账号 | stable SlotKey + lease/in-flight drain；归零前不 close |
| 流式中途 failover | SSE、tool call 或内容拼接损坏 | 首个下游 bytes 后只记录失败并终止当前流，禁止第二账号重放 |
| bootstrap 白名单过宽 | 未认证读取/修改管理数据 | method + path 精确矩阵；公开 shell 无状态；管理 API 认证回归测试 |
| Proxy Key 泄露取得 Admin 权限 | 客户端配置泄露导致凭据和账号被接管 | `QB2API_PROXY_API_KEY` 与 `QB2API_ADMIN_KEY` 分离；旧 Key 只映射 Proxy |
| Secure cookie 与 HTTP 不匹配 | 登录循环或误以为已有 TLS 保护 | `auto` 只对 loopback HTTP 降级；远程 HTTP 必须显式 `false`，并显示风险警告；受信 scheme 校验 |
| 远程管理暴露 | 凭据被控制 | Proxy/Admin Key + session + CSRF + HTTPS 优先；降级 HTTP 仅限 Tailscale/LAN ACL/防火墙 + 登录限流 |
| SQLite 阻塞事件循环/锁冲突 | Proxy、签到或 Admin 请求抖动/失败 | aiosqlite 单连接 worker、WAL、busy_timeout、短事务、version cache |
| Settings 双来源 | UI 显示已保存但 Scheduler 未变更 | runtime settings version + apply mode + operation id；环境变量只作为 fallback，禁止双写 |
| WorkBuddy status method 未确认 | 误发错误请求或误判签到 | status method 为空即跳过 preflight，CB-CHECKIN-01 后才启用 |
| 多账号触发风控 | 账号受限 | 串行 jitter，遵守服务条款，不做设备伪造 |
| SQLite 与主密钥不同步 | 数据不可恢复 | 绑定备份，校验 key，显式迁移 |
| Supervisor 按旧 PID/端口误杀进程 | 破坏其他服务或正在进行的请求 | 同时校验 PID、启动时间、owner、进程组和内部 token；不做端口盲杀 |
| Control Plane/Worker 版本漂移 | 握手失败或路由行为不一致 | protocol version、runtime snapshot schema 和启动前 ready gate；失败保持旧 Worker |
| Worker 停止导致管理台不可用 | 无法恢复服务或查看错误 | Control Plane 独立常驻，Worker 只作为受控子进程；service 页面显示真实 terminal state |
| Worker telemetry 阻塞模型请求 | 延迟上升或代理雪崩 | 有界异步队列、非阻塞写入、丢弃计数和健康告警 |
| 请求事件包含 Prompt/凭据 | 隐私泄漏 | event schema 白名单；只保留脱敏 metadata 和 token/latency |
| runtime settings 写入未生效 | UI 与实际调度/代理不一致 | value_version + apply_mode + operation id；新实例验证成功后才报告 applied |
| 前端只显示静态成功状态 | 运维误判服务、签到或积分 | Query 精确失效、轮询 operation、stale/unavailable 文案和失败详情 |
| 管理台复杂度造成不可用 | 高密度数据难以操作 | 桌面优先分域导航、表格筛选/抽屉、键盘可达、响应式窄屏降级和 Playwright 截图验收 |

## 20. 最终验收清单

### Control Plane 与服务生命周期

- [ ] Control Plane 可独立启动并持续提供 `/admin`、管理 API、历史和设置；Worker 停止/崩溃不会带走管理台。
- [ ] UI 可启动、停止、重启和 reload Proxy Worker，并显示 operation、desired/observed state、PID、uptime、in-flight、最近退出原因和健康状态。
- [ ] Supervisor 只操作 PID、启动时间、owner、进程组和内部 token 全部匹配的 Worker；端口冲突和 orphan 不会触发盲杀。
- [ ] Worker 只绑定 loopback，内部 API 只接受 loopback + internal token + protocol version；浏览器 session/Proxy Key 均不能调用。
- [ ] stop 先 drain，长流在 grace period 内可完成；超时终止只作用于已验证的同一进程组。

### 代理

- [ ] 只配置 `CODEBUDDY_TOKEN` 时原有 CodeBuddy chat 不变。
- [ ] 只配置 `QODER_TOKEN` 时现有 Qoder COSY chat 不变。
- [ ] CodeBuddy static、OAuth、manual Bearer 可混合轮询。
- [ ] Qoder 多 PAT 独立 session、轮询和失败隔离生效。
- [ ] Proxy Key 不能访问 Admin API；Admin Key 不能调用 Proxy API；两者相等时拒绝启动；旧 `QB2API_API_KEY` 只映射 Proxy。
- [ ] 动态启停/删除不需重启，已有流不被强制中断。
- [ ] 0、1、N slot 均使用同一稳定 pool；删除最后账号后模型请求返回明确 unavailable，不出现空列表崩溃。
- [ ] pool health/cooldown 使用 `(provider, account_id)`，不使用数组下标。
- [ ] OAuth Bearer refresh 后下一次请求无需重建 pool 即使用新值。
- [ ] env 与 DB 同凭据只产生一个有效实例，动态账号删除后 env slot 可恢复。
- [ ] promotion 生成随机持久账号 ID；Token/PAT 轮换只更新 credential version，不改变账号 ID。
- [ ] 首 chunk 前可 failover，首 chunk 后绝不换账号重放；cancel/retire 都释放 lease。

### 模型、用量与账号指标

- [ ] 前端可查看、筛选、启停、刷新和探测模型；模型变更通过 Worker reload 生效，历史 usage 不丢失。
- [ ] Overview/Usage 可按时间、Provider、模型和账号查看请求量、成功率、input/output tokens、p50/p95 latency 和错误趋势。
- [ ] request events 不含 prompt、completion、Authorization、Cookie 和上游原始响应；CSV 导出同样脱敏并写审计。
- [ ] Worker telemetry/rollup 故障不会让模型请求失败；丢弃事件数量在 service health 中可见。
- [ ] 账号页可查看 token 状态、过期、积分、quota、签到摘要和采样时间；stale/unknown/unavailable 不显示为 0。
- [ ] Metrics refresh 限频、单飞、失败退避；一个账号指标失败不影响其他账号和 Proxy。

### 登录和安全

- [ ] UI 可发起 OAuth，但浏览器看不到原始 Token。
- [ ] 未认证浏览器可加载登录 shell，但不能访问任何账号、签到、配置或 session 状态数据。
- [ ] session 登录限流、多会话上限、logout/logout-all、Admin Key 轮换撤销均有测试。
- [ ] admin session 只发送到 `/api/admin` 且不能认证 `/v1/*` 或 `/api/config`；cookie 变更请求必须通过 CSRF。
- [ ] 非 loopback 管理无 Admin Key 或主密钥时拒绝启动；`auto`/`true` 下无 HTTPS 拒绝创建 session，只有显式 `false` 允许受信 Tailscale/LAN HTTP 并显示风险警告。
- [ ] Qoder chat/check-in 导入只在受保护管理面接受，status/身份验证成功后 Secret 才加密落库。
- [ ] Cookie 只有真实契约需要时启用，普通前端不自动读取。
- [ ] API、UI、日志和错误响应无 Authorization、Cookie、Refresh Token 原文。
- [ ] `/admin/settings` 显示 schema、source、value version、apply mode 和生效状态；可写项经校验保存，应用失败不会显示成功。
- [ ] Proxy/Admin/internal token 权限分离；Proxy Key 可从 UI 创建、轮换、撤销和过期，但只展示一次明文且数据库只保存 hash。

### 签到

- [ ] WorkBuddy `HTTP 400 + code=10001` 为 `ALREADY_CHECKED_IN`，不重试。
- [ ] WorkBuddy 每账号独立 credential/身份头，不串 Cookie。
- [ ] Qoder status/claim/refresh 有真实 Spike 记录。
- [ ] Qoder 有可执行的 promotion -> Windows exporter -> HTTPS import -> 服务端验证 runbook；未 promotion/未导入分别显示 `needs_promotion`/`needs_import`。
- [ ] Qoder access 过期后 refresh 写回，失败进入 `NEEDS_REAUTH`。
- [ ] 单账号失败不阻断后续账号，不影响 chat 池。
- [ ] 同一账号 `chat=active/checkin=needs_reauth` 可同时存在，选择器只读取对应 purpose。
- [ ] purpose 的运行 status 与 verification_status 独立，未验证 check-in 不会被 scheduler 自动执行。
- [ ] `checkin-status` method 未配置时不会发送 preflight；daily-checkin 的 POST 和 `10001` 语义保持事实等级。
- [ ] Scheduler 每日本地时区单次执行，重启 catch-up 有边界。
- [ ] 手动与定时不并发，结果可按 run/account 查询。
- [ ] Phase 1 在完整运维 UI 之前已完成至少一个真实授权账号的自动签到验收。

### 工程质量

- [ ] Repository、Vault、OAuth、Proxy pool、两个 check-in client、scheduler、UI/API 有定向测试。
- [ ] Control/Worker factory、Supervisor 状态机、内部握手、drain、orphan reconcile 和 process identity 防护有定向测试。
- [ ] SQLite 并发读写、WAL、busy_timeout、migration 和 credential version 冲突有定向测试，事件循环没有同步 DB 调用。
- [ ] 旧 `/api/config` Token PATCH 兼容、deprecated 提示、restart_required 和不回显 Secret 均有测试。
- [ ] Vue 页面覆盖 Overview、Service、Accounts、Credentials、Models、Usage、Check-in、Settings、Audit；没有只有静态卡片或假按钮的空壳页面。
- [ ] 前端 unit/build、Playwright 登录/服务控制/账号/模型/设置流程、桌面与移动截图、无重叠和无控制台错误均有真实记录。
- [ ] `pytest`、`ruff check`、frontend lint/typecheck/test/build、Playwright、`git diff --check` 有真实执行记录。
- [ ] Mac Mini 重启、Worker 停止/异常退出、Control Plane 恢复、备份/restore dry-run 和数据库迁移回滚已验证。
- [ ] 实现中的“已确认/待验证”与本设计一致。

## 21. 决策摘要

1. `2api` 是 Mac Mini 上的常驻 Control Plane，独立管理受控 Proxy Worker；Worker 停止不会让管理台消失。
2. Vue 管理台覆盖服务生命周期、账号/凭据、模型、用量、Token/积分、签到、设置、审计和备份，不做普通用户/计费系统。
3. Control Plane、Worker、管理 API、Proxy API 和内部 RPC 使用不同边界与凭据；Proxy Key 泄露不能升级为 Admin 权限。
4. 前端 shell 可公开加载，但所有数据 API 受保护；敏感凭据由后端获取、验证、加密保存和按 purpose 使用。
5. CodeBuddy 采用 OAuth/static/manual 统一 chat 池；WorkBuddy 签到复用账号 ID，但独立认证和错误域。
6. Qoder chat 继续 PAT/COSY；Qoder check-in 默认 access/refresh 双凭证，直到 Spike 证明 PAT 可签到。
7. 每个 provider 始终注册支持 0..N 的 `DynamicProviderPool`，slot 以稳定复合键管理，并以 lease drain 安全退役。
8. 流式响应首个下游 bytes 后禁止跨账号重试；签到使用独立 `CheckinService + CheckinScheduler`，不复用 Proxy pool 语义。
9. SQLite 使用异步 Repository、WAL、短事务、runtime settings 版本和 credential version cache；purpose 状态独立保存。
10. Supervisor 使用 PID、启动时间、owner、进程组和内部 token 做生命周期防护，所有操作返回 operation id 并可审计。
11. Worker 请求事件、用量汇总和账号指标快照用于 Token/积分/配额监控；telemetry 故障不能阻塞模型响应，未知数据不伪造为 0。
12. 管理面采用精确 bootstrap 白名单、限流 session、CSRF 和远程 HTTPS；session cookie 永不认证 proxy 或 `/api/config`。
13. env static slot 只是兼容来源；promotion 生成随机持久账号 ID，Token/PAT 轮换不改 ID，旧 `/api/config` Token PATCH 经过明确弃用周期。
14. Qoder 首次签到凭据通过 promotion 后的随机账号绑定 Windows exporter 或手动最小 payload 导入；导入后 Mac Mini 独立 refresh/claim。
15. runtime settings 采用 schema、value_version 和 apply mode，UI 只有在实际应用成功后才显示生效；未确认的 WorkBuddy status method 不发请求。
16. 实施按 Control/Worker -> 账号/管理台 -> 模型/用量 -> 签到 -> Qoder -> 运维硬化推进，每阶段都必须有可回归的纵向结果。
17. 未验证的 WorkBuddy URL/auth、CodeBuddy refresh、Qoder PAT claim 和 refresh rotation 先做 Spike，再开启自动化。
