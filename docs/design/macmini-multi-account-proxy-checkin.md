# 2api 统一账号池、多账号代理与双端自动签到重构设计

> 状态：设计基线，未进入代码实现
>
> 审查状态：已于 2026-07-22 完成两轮外部设计审查吸收；实现前架构契约已闭合，仍须先完成 Phase 0 回归基线与真实账号 Spike
>
> 适用部署：远程 Mac Mini 单实例常驻服务，客户端通过 OpenAI / Anthropic 兼容接口访问
>
> 本文是 `docs/design` 的唯一设计方案。它合并了 CodeBuddy OAuth 池、WorkBuddy 签到和 Mac Mini 双端代理/签到三份设计，并以当前 `2api` 与本地参考工程源码为事实依据。

## 1. 执行摘要

### 1.1 目标

把当前只支持环境变量静态 Token 的 `2api`，重构为一个运行在 Mac Mini 上的统一服务：

```text
2api = OpenAI/Anthropic Proxy
     + CodeBuddy 多账号代理池
     + Qoder 多账号代理池
     + CodeBuddy/WorkBuddy 自动签到
     + Qoder 自动签到
     + 本地管理 UI
     + 统一账号、凭据、状态和运维 API
```

客户端只需要访问 Mac Mini 的 `/v1` 或 Anthropic 兼容端点，日常不再打开 CodeBuddy、WorkBuddy 或 QoderWork。账号登录、凭据刷新、代理轮询和每日签到都由同一个 FastAPI 进程编排。

### 1.2 核心决策

| 决策 | 结论 |
| --- | --- |
| 服务形态 | 保留 Python/FastAPI `2api` 为唯一常驻服务，不重写 Rust 客户端，不依赖第二个常驻进程 |
| 管理入口 | FastAPI 同源静态 Web UI + `/api/admin/*` 管理 API |
| 凭据边界 | 前端发起登录或提交导入，原始 Bearer、Refresh Token、Cookie 只在后端和加密存储中出现 |
| CodeBuddy chat | `ck_`、OAuth Bearer、手动 Bearer 进入同一个 CodeBuddy 代理账号池 |
| WorkBuddy/CodeBuddy 签到 | 复用统一账号 ID，但签到凭据、状态、失败域独立于 chat |
| Qoder chat | 继续使用现有 `pt_` PAT + COSY session 代理链 |
| Qoder check-in | 默认使用桌面会话 access/refresh 双凭证；不假设 `pt_` 可以直接签到 |
| 调度器 | 一个进程内 `asyncio` 调度器；按账号串行执行，单号失败不阻断其他账号 |
| 持久化 | SQLite 保存账号元数据、purpose 级状态和签到结果；凭据字段使用 `cryptography` 加密 |
| 远程安全 | Proxy 与 Admin 使用不同 Key；非 loopback 管理面必须配置 Admin Key、凭据加密主密钥和 HTTPS，优先通过 Tailscale Serve/反向代理或 SSH loopback 访问 |
| 外部契约 | WorkBuddy 路径/鉴权、Qoder `pt_` 是否可签到、refresh 轮换规则必须通过真实账号 Spike 后才可标记为已验证 |

### 1.3 非目标

- 不把浏览器 Cookie 自动抓取误包装成普通网页能力。跨域 `HttpOnly Cookie` 不能由前端 JavaScript 读取。
- 不把 Cookie、Bearer 或 Refresh Token 返回给浏览器、写入 URL、LocalStorage、普通日志或 `/api/config`。
- 不做验证码、CAPTCHA、扫码风控绕过、设备伪造、账号限制规避或多实例分布式控制平面。
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
2api @ Mac Mini
  |-- Admin UI + Admin API + Admin Session
  |-- AccountRegistry
  |     |-- CredentialVault
  |     |-- AccountRepository
  |     `-- Capability Matrix
  |-- CredentialResolver
  |-- Proxy Pools
  |     |-- CodeBuddy chat
  |     `-- Qoder chat/COSY
  `-- CheckinService + CheckinScheduler
        |-- WorkBuddy/CodeBuddy
        `-- Qoder status/claim/refresh
```

### 4.2 责任边界

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| `AccountRegistry` | 账号元数据、启停、能力、稳定 ID、状态汇总 | 直接发上游 HTTP |
| `CredentialVault` | 加密保存/读取 Secret、原子更新、权限检查 | 决定业务能力 |
| `CredentialResolver` | 按 provider/account/purpose 返回临时凭据，单飞 refresh | 返回凭据给 UI |
| `ProxyPool` | chat 账号选择、冷却、401 重试和动态重建 | 签到顺序或幂等 |
| `CheckinService` | 单账号/批次签到、分类、落库、隔离 | 计算下一次时间 |
| `CheckinScheduler` | 时区、每日窗口、补偿、批次锁、生命周期 | 构造 HTTP 请求 |
| `OAuthBroker` | CodeBuddy auth HTTP 和 flow 状态 | 多账号轮询或 UI 渲染 |
| `Admin UI` | 展示脱敏状态、触发动作、收集输入 | 读取跨域 Cookie 或保存 Secret |

### 4.3 单进程与并发边界

- 一个 FastAPI 进程只启动一个 Registry、一个 Scheduler 和一组 Provider pool。
- Proxy 可并发；单账号 Token refresh 由 purpose 级 `asyncio.Lock` 单飞。
- 签到批次全局互斥；账号默认串行并带 jitter。
- 每个 provider 家族始终注册一个支持 0..N slot 的 `DynamicProviderPool`；热更新只替换不可变 active snapshot。
- slot 以 `(provider, account_id)` 为稳定键；退役 slot 停止接受新请求，保留到 in-flight lease 归零后再关闭。
- 本版不做跨进程锁、leader election、租约或分布式状态。

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
```

OAuth 原始 state 只在进程内或加密保存，数据库最多保存 hash，防止重放。管理 session 和 CSRF 原值只存在于 cookie/响应及进程内，数据库只保存 hash；Admin Key 轮换时撤销全部 session。

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
- 远程管理必须使用 HTTPS；优先 Tailscale Serve/受信反向代理。HTTP 只允许 SSH 转发后的 loopback 开发访问。
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
- `false`：仅允许 loopback 开发/SSH 隧道；若请求来自非 loopback，启动或请求校验失败，不能作为远程部署逃生开关。

因此生产拓扑必须是 `https://<tailscale-host>/admin`；直接 `http://<tailscale-ip>:9999/admin` 不是受支持的远程会话方式。不把 API Key 放进 URL、LocalStorage 或 SessionStorage。

### 7.3 UI 页面

MVP 使用 FastAPI 同源静态资源，避免第二个 Node 常驻服务。第一阶段采用原生 HTML/CSS/ES modules；只有交互复杂度真实超过静态页面时才引入 Vite/React。匿名打开只显示登录页；登录后才调用管理 API 加载账号数据。

```text
/admin
  Proxy 可用账号、今日签到、下次调度、needs_reauth

/admin/accounts
  provider、来源、能力、Proxy/签到状态、过期时间
  启停、删除、重新登录、刷新、探测能力

/admin/accounts/add
  CodeBuddy OAuth / 手动 Bearer
  Qoder PAT / check-in access+refresh
  WorkBuddy Cookie/身份头（仅实测需要时显示）

/admin/checkin
  今日批次、每账号结果、手动全量/指定执行

/admin/settings
  只读显示有效调度时间、时区、重试和功能开关
  标记 source=env/default，并提示修改环境变量后重启
```

MVP 不提供 runtime settings 写入：没有 settings 表、没有 `PATCH /api/admin/settings`，Scheduler 只在 lifespan 启动时读取经过校验的环境配置。这样避免出现“UI 已保存但 Scheduler 仍按旧时间运行”的双配置源。未来若确需热更新，必须另行设计 `runtime_settings`、版本化校验和原子 reschedule，不在本重构中暗含实现。

### 7.4 CodeBuddy OAuth UI 流程

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

### 7.5 上游凭据 Cookie 边界

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
```

使用 `zoneinfo.ZoneInfo`，不增加 cron 解析依赖。

### 11.2 生命周期

```text
lifespan enter
  load settings
  open SQLite and CredentialVault
  load AccountRegistry
  create chat pools
  create one CheckinScheduler task when enabled
  build model index
  yield
lifespan exit
  cancel and await scheduler
  close checkin clients
  close retired/current providers
  close SQLite
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

### 12.5 只读设置

```text
GET /api/admin/settings
  -> effective check-in/scheduler/provider settings
  -> 每项包含 value、source=env|default、restart_required_to_change=true
```

响应不含任何 Key、Token、Cookie、主密钥或完整上游身份。MVP 没有 PATCH；UI 不得伪造保存按钮。环境变量修改后通过受控服务重启原子创建新的 Scheduler，不实现运行时 cancel/reschedule。

## 13. 配置

### 13.1 服务和存储

```ini
QB2API_HOST=0.0.0.0
QB2API_PORT=9999
QB2API_PROXY_API_KEY=<optional-for-legacy-open-proxy>
QB2API_ADMIN_KEY=<required-for-admin-ui-dynamic-checkin>
# QB2API_API_KEY=<deprecated-proxy-alias>
QB2API_DATA_DIR=./data
QB2API_CREDENTIAL_KEY=<fernet-key-required-for-persistent-secrets>
QB2API_ADMIN_UI_ENABLED=true
QB2API_ADMIN_UI_PATH=/admin
QB2API_ADMIN_COOKIE_SECURE=auto
QB2API_ADMIN_SESSION_TTL_HOURS=12
QB2API_ADMIN_SESSION_IDLE_MINUTES=60
QB2API_TRUSTED_PROXY_HEADERS=false
QB2API_TRUSTED_PROXY_NETWORKS=<optional-CIDR-list>
```

保留 `0.0.0.0` 兼容 Mac Mini proxy。启用管理 UI、动态凭据或签到时无 `QB2API_ADMIN_KEY`/主密钥拒绝启动；非 loopback 管理还必须满足 7.2 的 HTTPS/cookie 契约。Proxy Key 可以为空以兼容旧的开放 proxy，但远程部署应显式配置 `QB2API_PROXY_API_KEY`。旧 `QB2API_API_KEY` 只映射 Proxy 权限；配置了两个新 Key 且值相同必须拒绝启动。

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
  accounts/
    models.py             # AccountRecord, AccountView, Capability, Status, VerificationStatus
    repository.py         # aiosqlite single connection, WAL, transaction/version API
    vault.py              # Fernet encrypt/decrypt and key validation
    registry.py           # merge env + DB, enable/disable, snapshots
    resolver.py           # purpose credential, skew refresh, single-flight
  admin/
    auth.py               # Proxy/Admin key policy and HttpOnly session/CSRF
    router.py             # /api/admin/* routes
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
  providers/
    codebuddy.py          # account-backed chat provider
    qoder.py              # account-backed PAT/COSY provider
    lb.py                 # DynamicProviderPool(0..N), SlotHandle lease, stream commit, drain
  web/
    admin.html
    admin.js
    admin.css
  app.py                  # lifecycle and router assembly
  config.py               # environment binding and validation

pyproject.toml            # add aiosqlite runtime dependency

tools/
  qoder-checkin-exporter/ # Windows one-shot exporter; 不随服务常驻
```

保持现有 `Provider` 抽象和 OpenAI/Anthropic 转换层不变。先分离账号、凭据、管理和签到，再接入 Provider；`app.py` 只装配，不增加具体协议细节。

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
10. **API/UI**：Proxy/Admin Key 权限矩阵、bootstrap、登录限流、session/CSRF、Secure cookie、promotion、settings 只读、无 Secret 响应、409。
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

### Phase 0：基线和外部契约 Spike

- 固化当前 `2api` 测试基线和配置兼容行为。
- 完成 CB-CHECKIN-01、QD-CHECKIN-01、AUTH-01。
- 固化 Proxy/Admin Key 权限矩阵、15.2 `/api/config` 兼容响应和现有 private route 回归测试。
- 为当前已确认的 pool 缺陷先增加回归用例：0 slot、删除最后 slot、按稳定 key 冷却、首 chunk 后禁止 failover。
- 记录两个本地参考工程 commit。
- 输出脱敏“已验证/未验证”矩阵。

未通过时，不实现依赖未知契约的自动 refresh 或 Cookie 自动化。

### Phase 1：首个可用纵向闭环（CodeBuddy + WorkBuddy）

只实现交付首个自动签到和多账号代理所需的最小底座：

- 加入 `aiosqlite`，建立 `accounts/account_purposes/credentials/checkin_daily_state/admin_sessions` 最小 schema、WAL Repository、Vault、Registry 和带 version cache 的 CredentialResolver。
- 兼容 env static slot 和旧 `/api/config`；实现动态优先去重，但不做完整迁移后台。
- 实现 Proxy/Admin Key 分离、7.1/7.2 的 bootstrap、session、CSRF、限流和最小 UI，仅含登录、账号列表、CodeBuddy 添加和今日签到。
- 接入 CodeBuddy OAuth/manual 账号、逐请求 resolver 和稳定 `DynamicProviderPool(0..N)`；实现首 chunk 提交边界和 slot lease drain。
- 按 CB-CHECKIN-01 实现 WorkBuddy client、`10001 -> ALREADY_CHECKED_IN`、最小 scheduler、手动运行和 daily state。
- 完成 Secret 不泄漏、purpose 故障隔离和旧 OpenAI/Anthropic proxy 回归测试。

明确后置：完整筛选、历史浏览、设置页、备份 UI、通用 capability 编辑器和高级审计。它们不能挡住第一条自动签到。

验收：旧 env-only proxy 行为不变且旧 Key 只有 Proxy 权限；0/1/N 动态池切换无需替换 Registry；partial stream 不跨账号；至少两个 CodeBuddy 账号可轮询；至少一个授权账号在 Mac Mini 完成定时或 catch-up 签到；已签到不重试；签到错误不冷却 chat；远程 UI 只能由 Admin Key 经 HTTPS 登录。

### Phase 2：账号平台和 CodeBuddy 池硬化

- 补齐 schema migration、Repository 并发/版本冲突测试、OAuth flow 恢复/过期清理和凭据轮换。
- 完成 CodeBuddy 401 refresh/re-auth、账号动态启停/删除、promotion、retirement 压力测试和流式取消验证。
- 补齐账号详情、purpose 状态、探测/刷新和基础签到历史，不引入第二个前端服务。
- 验证 `/api/config` deprecated 响应、env/DB shadow 行为和迁移说明。

验收：Token refresh 不需重建 pool；单号失效不影响其他账号；UI、API、日志和数据库元数据无 Secret 原文。

### Phase 3：Qoder 多 PAT Proxy 强化

- 将现有 QoderProvider 纳入 AccountRegistry，保留 COSY 协议行为。
- 每 PAT 独立 session、失效重建、失败隔离、动态启停。
- 保证 chat、工具调用和 CodeBuddy/WorkBuddy 已交付能力回归。

验收：多个 PAT 轮询；一个 PAT/COSY 失败不影响其他 PAT、CodeBuddy 或签到调度器。

### Phase 4：Qoder 首次导入与自动签到闭环

- 交付并审计 10.3 的 Windows one-shot exporter、最小 JSON schema 和导入 runbook；exporter 不进入 Mac Mini 常驻进程。
- 实现受保护的 access/refresh import、服务端 status 验证、加密保存和身份匹配。
- 实现 purpose refresh、status/claim、refresh rotation 持久化和 `needs_reauth` UI。
- 只有 QD-CHECKIN-01 证明后才允许 PAT 合并；默认继续双凭证。

验收：完成 promotion + 标准导入后，Mac Mini 无 QoderWork GUI 也能跨 access 过期连续运行；refresh 失败清晰可见且不影响 chat；env-only 账号保持 `needs_promotion`，已 promotion 但缺少 access/refresh 的账号保持 `needs_import`。

### Phase 5：运维和体验硬化

- 完成账号筛选、完整签到历史、限流可观测性、备份/恢复和 Key 轮换说明。
- 管理操作审计只记账号 ID、purpose 和动作，不记 Secret。
- 验证 Tailscale Serve/受信代理、SSH loopback、升级回滚、数据库迁移和 session 撤销。
- 删除已结束的兼容行为必须单独评审；不能在本阶段顺手移除旧 `/api/config` token 字段。

以上阶段是依赖顺序，不要求把“平台模块”整批做完才验收业务。每个 Phase 必须有可运行纵向结果；Phase 1 的首个自动签到是全项目最早业务门禁。

## 18. 部署拓扑与运行手册

```text
[Mac Mini]
  2api: 127.0.0.1:9999
  SQLite + encrypted credentials: ./data/
  one FastAPI process
  one in-process CheckinScheduler

[HTTPS ingress]
  Tailscale Serve 或受信 Caddy/nginx
  https://<tailscale-host> -> http://127.0.0.1:9999
  只从受信 ingress 接受 forwarded scheme

[开发/维护电脑]
  浏览器访问 https://<tailscale-host>/admin
  通过 auth_url 完成 CodeBuddy 登录
  按一次性 Windows exporter runbook 导入 Qoder check-in access/refresh
  必要时手动导入 WorkBuddy Cookie

[本地开发工具]
  OPENAI_BASE_URL=https://<tailscale-host>/v1
  ANTHROPIC_BASE_URL=https://<tailscale-host>
  API key = QB2API_PROXY_API_KEY
```

启动检查：

1. Proxy Key、Admin Key、凭据主密钥来自不同的安全配置项，两个 Key 不相同；旧 Key 只映射 Proxy。
2. data 目录权限正确。
3. SQLite schema 迁移成功。
4. static/dynamic 账号数量和 capability 符合预期。
5. scheduler 只有一个 task，下一次本地时间正确。
6. 远程入口为 HTTPS，`QB2API_ADMIN_COOKIE_SECURE` 结果与代理 scheme 一致。
7. 匿名只能取得 UI shell，所有账号数据 API 均返回未认证；admin session 不能调用 proxy API。

Qoder 首次开通：

1. Windows 上登录目标 QoderWork CN 账号并关闭可能并发改写 profile 的切换操作。
2. 运行版本和 commit 可追溯的 one-shot exporter，得到最小 JSON；确认输出没有 PAT、COSY session 或完整 profile。
3. 通过 HTTPS 管理页把 JSON 绑定到正确的持久 Qoder `account_id`；只有 env slot 时先 promotion。
4. 等待服务端 status 验证成功并显示 `checkin=active`；失败时保留旧 credential，按脱敏错误处理。
5. 删除临时 JSON；次日和 access 过期后分别核验 scheduler/refresh 记录，之后不再需要 Windows 或 QoderWork 常驻。

日常维护：

- 只通过 `/admin` 查看状态和重新授权，不手工编辑数据库。
- `needs_reauth` 先重新登录/导入，再恢复对应 purpose。
- 修改 endpoint/path 必须重做对应 Spike。
- 备份数据库时同时备份主密钥；缺少主密钥的备份不可恢复。
- env Token 列表只用于 transient chat；需要长期身份、签到或审计时先 promotion，不依赖 `cb-env-N/qd-env-N` 顺序。

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
| Secure cookie 与 HTTP 不匹配 | 登录循环或降级为不安全 cookie | 远程 HTTPS 强制；`auto` 只对 loopback HTTP 降级；受信 scheme 校验 |
| 远程管理暴露 | 凭据被控制 | Proxy/Admin Key + session + CSRF + HTTPS/Tailscale/SSH + 登录限流 |
| SQLite 阻塞事件循环/锁冲突 | Proxy、签到或 Admin 请求抖动/失败 | aiosqlite 单连接 worker、WAL、busy_timeout、短事务、version cache |
| Settings 双来源 | UI 显示已保存但 Scheduler 未变更 | MVP settings 只读，环境变量修改后重启；不伪造 runtime PATCH |
| WorkBuddy status method 未确认 | 误发错误请求或误判签到 | status method 为空即跳过 preflight，CB-CHECKIN-01 后才启用 |
| 多账号触发风控 | 账号受限 | 串行 jitter，遵守服务条款，不做设备伪造 |
| SQLite 与主密钥不同步 | 数据不可恢复 | 绑定备份，校验 key，显式迁移 |

## 20. 最终验收清单

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

### 登录和安全

- [ ] UI 可发起 OAuth，但浏览器看不到原始 Token。
- [ ] 未认证浏览器可加载登录 shell，但不能访问任何账号、签到、配置或 session 状态数据。
- [ ] session 登录限流、多会话上限、logout/logout-all、Admin Key 轮换撤销均有测试。
- [ ] admin session 只发送到 `/api/admin` 且不能认证 `/v1/*` 或 `/api/config`；cookie 变更请求必须通过 CSRF。
- [ ] 非 loopback 管理无 Admin Key、主密钥或 HTTPS 时拒绝启动/创建 session。
- [ ] Qoder chat/check-in 导入只在受保护管理面接受，status/身份验证成功后 Secret 才加密落库。
- [ ] Cookie 只有真实契约需要时启用，普通前端不自动读取。
- [ ] API、UI、日志和错误响应无 Authorization、Cookie、Refresh Token 原文。
- [ ] `/admin/settings` 只读显示 env/default 来源，没有未实现的保存按钮；修改配置需重启。

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
- [ ] SQLite 并发读写、WAL、busy_timeout、migration 和 credential version 冲突有定向测试，事件循环没有同步 DB 调用。
- [ ] 旧 `/api/config` Token PATCH 兼容、deprecated 提示、restart_required 和不回显 Secret 均有测试。
- [ ] `pytest`、`ruff check`、`git diff --check` 有真实执行记录。
- [ ] Mac Mini 重启、服务关闭、备份恢复流程已验证。
- [ ] 实现中的“已确认/待验证”与本设计一致。

## 21. 决策摘要

1. `2api` 是 Mac Mini 上的单服务中枢，UI、代理和签到共享账号底座。
2. 前端 shell 可公开加载，但所有数据 API 受保护；敏感凭据由后端获取、验证、加密保存和按 purpose 使用。
3. CodeBuddy 采用 OAuth/static/manual 统一 chat 池；WorkBuddy 签到复用账号 ID，但独立认证和错误域。
4. Qoder chat 继续 PAT/COSY；Qoder check-in 默认 access/refresh 双凭证，直到 Spike 证明 PAT 可签到。
5. Proxy/Admin 使用独立 Key；旧 `QB2API_API_KEY` 只映射 Proxy，Admin session 只能由 Admin Key 建立。
6. 每个 provider 始终注册支持 0..N 的 `DynamicProviderPool`，slot 以稳定复合键管理，并以 lease drain 安全退役。
7. 流式响应首个下游 bytes 后禁止跨账号重试；签到使用独立 `CheckinService + CheckinScheduler`，不复用 Proxy pool 语义。
8. SQLite 使用 aiosqlite 单连接、WAL、短事务和 credential version cache；`account_purposes` 独立记录运行 status 与 verification_status。
9. 管理面采用精确 bootstrap 白名单、限流 session、CSRF 和远程 HTTPS；session cookie 永不认证 proxy 或 `/api/config`。
10. env static slot 只是兼容来源；promotion 生成随机持久账号 ID，Token/PAT 轮换不改 ID，旧 `/api/config` Token PATCH 经过明确弃用周期。
11. Qoder 首次签到凭据通过 promotion 后的随机账号绑定 Windows exporter 或手动最小 payload 导入；导入后 Mac Mini 独立 refresh/claim。
12. MVP settings 只读，环境配置通过受控重启生效；未确认的 WorkBuddy status method 不发请求。
13. Phase 1 优先交付 CodeBuddy 多账号 + WorkBuddy 自动签到纵向闭环，完整平台体验后置但安全底线不后置。
14. 未验证的 WorkBuddy URL/auth、CodeBuddy refresh、Qoder PAT claim 和 refresh rotation 先做 Spike，再开启自动化。
