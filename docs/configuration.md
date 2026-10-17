# 配置指南

[README.zh](../README.zh.md)；部署方式为官方 Docker 镜像，见 [Docker 部署](../README.md#docker-deployment)。

## 1. 三类密钥

系统按信任域拆成三个互不相同的随机值，缺一不可：

| 变量 | 作用 | 谁持有 |
| --- | --- | --- |
| `QB2API_PROXY_API_KEY` | 模型客户端请求代理的凭据 | 客户端 / CLI |
| `QB2API_ADMIN_KEY` | 管理台登录与管理员自动化 | 运维者 |
| `QB2API_CREDENTIAL_KEY` | 持久化账号凭据的静态加密密钥（Fernet） | 仅 Control Plane |

本机生成：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'   # Proxy Key
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'   # Admin Key
python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'  # Credential Key
```

安全边界：

  存储、Git、截图、服务文件或普通日志。
- 丢失 `QB2API_CREDENTIAL_KEY` 后无法解密已存动态凭据；轮换它不会迁移旧数据。
- `QB2API_API_KEY` 是已废弃的 Proxy-only 别名，不要当作 Admin Key 使用。

## 2. 环境变量参考（.env）

从 `.env.example` 复制后按需修改：

```bash
cp .env.example .env
chmod 600 .env
mkdir -p data logs && chmod 700 data logs
```

### 核心

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `QB2API_HOST` / `PORT` | `0.0.0.0` / `9999` | 监听地址。单进程同时提供代理、管理 API 与管理台 |
| `QB2API_DATA_DIR` / `LOG_DIR` | `./data` / `./logs` | SQLite、备份、日志目录（运行用户独占） |
| `QB2API_MODEL_CONFIG` | `./config/models.json` | 模型配置路径 |
| `QB2API_WEB_DIR` | 自动探测 | 管理台静态资源目录；默认取可执行文件旁的 `web/dist` |
| `QB2API_EVENT_FLUSH_MILLIS` | `250` | 遥测批写窗口（毫秒）。调大 = 更少、更大的写事务 |
| `QB2API_EVENT_FLUSH_MAX` | `200` | 单批事件上限，达到即立即刷写 |

> `QB2API_CONTROL_PORT` / `QB2API_WORKER_PORT` / `QB2API_MODE` 等双进程时代的变量仍会被读取，
> 便于沿用旧 `.env`，但单进程下只有 `QB2API_PORT` 决定实际监听端口。

### 管理台与远程访问

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `QB2API_ADMIN_UI_ENABLED` | `true` | 管理台开关 |
| `QB2API_ADMIN_UI_PATH` | `/admin` | 管理台路径 |
| `QB2API_ADMIN_COOKIE_SECURE` | `auto` | `auto`=本机 HTTP / 远程 HTTPS；`false`=显式受信 HTTP；`true`=一律 HTTPS |
| `QB2API_ADMIN_SESSION_TTL_HOURS` / `IDLE_MINUTES` | `12` / `60` | 会话有效期 |
| `QB2API_TRUSTED_PROXY_HEADERS` | `false` | 仅当明确 HTTPS 反向代理直连对端时开启 |

### 账号、模型与签到

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `CODEBUDDY_TOKEN` / `WORKBUDDY_INTL_TOKEN` | 空 | 旧式静态 token（transient chat slot）；长期账号请在管理台导入 |
| `WORKBUDDY_INTL_ENDPOINT` | `https://www.workbuddy.ai` | WorkBuddy 国际版入口 |
| `WORKBUDDY_INTL_OAUTH_ENABLED` | `true` | 是否允许管理台发起国际版浏览器登录 |
| `WORKBUDDY_INTL_CREDITS_PATH` | `/billing/meter/get-user-resource` | 国际版积分查询路径 |
| `QB2API_INTL_DEFAULT_REASONING_EFFORT` | `low` | 客户端未传 `reasoning_effort` 时注入的档位 |
| `QB2API_CREDENTIAL_REFRESH_ENABLED` | `true` | 凭据主动轮换开关 |
| `QB2API_CREDENTIAL_REFRESH_INTERVAL_SECONDS` | `900` | 轮换扫描间隔（秒），决定检查频率而非刷新频率 |
| `QB2API_CREDENTIAL_REFRESH_LEAD_SECONDS` | `1800` | 到期前多少秒必须刷新（绝对下限） |
| `QB2API_CREDENTIAL_ROLLING_REFRESH_HOURS` | `6` | 滚动刷新周期（小时）：无论剩余有效期多久，超过该时长即刷新 |
| `CHECKIN_ENABLED` | `false` | 全局签到调度开关（也可在管理台设置） |
| `CHECKIN_AT` / `CHECKIN_TIMEZONE` | `00:10` / `Asia/Shanghai` | 每日签到时间与时区 |
| `GROWTH_AUTO_ACTIVE_DAY` | `true` | 活跃日自动化开关；由成长调度器每日执行一次正式对话点亮 |

其余签到/指标/用量变量见 `.env.example` 内注释；管理台「设置」页可持久化运行时配置
（签到时间、成长自动化开关等），优先级高于启动默认值。

## 凭据续期

WorkBuddy 的 access token 自带 28 天 `exp`，但**上游会在远早于此的时刻作废整个会话**。
2026-10-17 实测：四个账号 10-06 登录，JWT 到期日 11-13，其中三个在 10-17 14:37 UTC
被上游批量吊销，此后 `401`，刷新返回
`invalid_grant: Refresh token issued before the user session started`。第四个账号因
session 签发时间晚 65 秒而存活——说明作废是按时间点批量执行的，与 token 自身声明无关。

因此**只按到期时间刷新是无效的**：30 分钟提前量对 28 天 token 意味着永不触发。
当前实现有三条触发路径：

| 触发 | 条件 | 作用 |
| --- | --- | --- |
| 滚动刷新 | 距上次写入超过 `QB2API_CREDENTIAL_ROLLING_REFRESH_HOURS`（默认 6 小时） | 主力。保持上游会话活跃，并限制本地凭据的最大陈旧度 |
| 比例窗口 | 剩余有效期 ≤ 总有效期的 20% | 自适应短效 token（2 小时 token 提前 24 分钟） |
| 绝对下限 | 剩余有效期 ≤ `QB2API_CREDENTIAL_REFRESH_LEAD_SECONDS` | 历史规则，作为兜底 |
| 请求路径抢救 | 上游返回 401 | 立即异步刷新，同账号 60 秒内去重；不等下一轮扫描 |

管理台「设置」页的 `credential.refresh_interval_hours`（1–72，默认 6）可热调整滚动周期，
无需重启。

### 刷新失败的两种处理

上游对同一个刷新接口返回两类错误，处理方式相反：

| 上游响应 | 判定 | 处理 |
| --- | --- | --- |
| `invalid_grant: Refresh token issued before the user session started`<br>`invalid_grant: Offline user session not found` | 会话已被上游吊销，无法自愈 | 账号 purpose 置 `needs_reauth`、移出代理池；保留凭据行，每滚动周期重试一次，上游若恢复会话则自动复活 |
| 裸 `12153:refresh token is invalid`、5xx、网络错误 | 瞬时故障（实测同一 token 会自行恢复） | 只记一次日志，下轮扫描继续重试，账号状态不变 |

`12153` **不**单独作为会话死亡的判据：2026-10-06 实测该码会瞬时出现并在下次请求成功。
只有带 `invalid_grant:` 前缀的两种文本才判定为不可恢复。

刷新成功但响应缺少 `expiresIn` 时保留原有到期时间，避免把到期时间写成空值后触发刷新风暴。

### 按模型的提供商路由权重

同一个统一模型 ID 可能同时由多个提供商提供（例如 `deepseek-v4.1-flash` 在国内版和
国际版都存在）。管理台「路由策略」页可对每个模型设置：

| 字段 | 含义 |
| --- | --- |
| `priority` | 越小越先尝试；不同取值构成有序梯队 |
| `weight` | 同一梯队内的相对流量占比（平滑加权轮询） |
| `enabled` | 关闭后仅在其他路由都不可用时才兜底 |

典型配置：给 `deepseek-v4.1-flash` 的 `workbuddy_intl` 设 `priority=0`、
`codebuddy` 设 `priority=1`，即国际版免费额度优先，请求失败再回落国内账号。
未配置策略的模型保持默认行为（同梯队等权重轮询）。修改后立即生效，无需重启。

注意两条路线的**有效上下文上限不同**：国际版上游把上下文窗口按
`prompt_tokens + min(max_tokens, 384000) ≤ 1048576` 判定，因此有效输入上限约 664K；
国内路线不套用该规则，同批请求可到 780K 以上。

### 账号级路由策略与会话保持

上一节是「模型 → 提供方」，这一节是同一提供方内「多账号 → 请求」。两者串联生效：
先按提供方策略选出路线，再在该提供方内按账号策略选账号。

在管理台「运行设置 · 账号路由」配置，全部热应用（写入 SQLite，立即推送到代理层）：

| 设置项 | 默认 | 含义 |
| --- | --- | --- |
| `routing.account_strategy` | `round_robin` | `round_robin` 均分；`weighted` 按账号权重比例分配（平滑加权轮询，3:1 会交错而非突发）；`fill_first` 按权重从大到小，前一个账号优先承接全部流量 |
| `routing.session_affinity` | `false` | 会话保持。开启后，同一轮提问的多条请求（含工具调用回环）固定同一账号 |
| `routing.session_affinity_ttl_seconds` | `3600` | 会话绑定空闲超时，最小 60 秒 |

账号权重在「账号详情 · 用途与路由 · 路由权重」设置，范围 1–1000，默认 100。`round_robin`
忽略权重；`weighted` 按比例；`fill_first` 把它当优先级（值大的先用，值相同则按并发数错峰）。

**会话保持的键是「系统提示 + 客户端 user 字段 + 最新一条 user 消息」**：agent 客户端每轮
重发整段历史，首条 user 消息永远不变，所以键取最新提问——同一个工具回环里提问不变，
键稳定；用户提出新问题，键立即变化并按策略重选账号。这也是开启会话保持的实际收益：
上游 prompt cache 是「前缀 + 账号」维度，一轮对话散到多个账号会各自付一次冷前缀。

绑定只在账号可用时生效：账号被手动停用、配额阻塞或上游失败时会立刻改选其他账号，
不会因为会话保持而卡死请求。不配置这两项时行为与历史版本完全一致（均分轮询、无绑定）。

## 3. 统一入口与端口

客户端只需要一个地址：

| 用途 | 地址 |
| --- | --- |
| OpenAI base URL | `http://127.0.0.1:9999/v1` |
| Anthropic Messages | `http://127.0.0.1:9999/v1/messages` |
| 模型列表 | `http://127.0.0.1:9999/v1/models` |
| 管理台 | `http://127.0.0.1:9999/admin/` |
| 健康检查 | `http://127.0.0.1:9999/health` |

`/v1/*` 由 Control Plane 转发到 loopback Worker；直连 `http://127.0.0.1:10001/v1`
仍可用作兼容地址。模型 ID 为统一小写（如 `deepseek-v4-flash`、`glm-5.2`、
`qwen3.7-max`），不再带 `provider/` 前缀；两端共有模型内部按提供商轮询，首块输出前
自动故障转移。

## 4. 远程访问配置（可复制）

### 受信 Tailscale/LAN HTTP（显式降级）

```ini
QB2API_CONTROL_HOST=<本机可信 Tailscale 或 LAN IP>
QB2API_CONTROL_PORT=9999
QB2API_ADMIN_COOKIE_SECURE=false
QB2API_TRUSTED_PROXY_HEADERS=false
```

仅适用于可信私网；绝不用于公网 DNS、端口转发、共享 Wi-Fi 或公共反向代理。

### HTTPS 反向代理（推荐）

```ini
QB2API_CONTROL_HOST=127.0.0.1
QB2API_ADMIN_COOKIE_SECURE=auto
QB2API_TRUSTED_PROXY_HEADERS=true
QB2API_TRUSTED_PROXY_NETWORKS=127.0.0.1/32
```

反向代理必须覆盖 `X-Forwarded-For` 与 `X-Forwarded-Proto`；不要对宽泛网段或任意客户端
开启该信任。

官方部署方式是 Docker 镜像（支持 `linux/amd64` 与 `linux/arm64`），推荐使用
[`docker-compose.yml`](../docker-compose.yml) 一键启动：

```bash
docker compose up -d
```

镜像可从 [GHCR](https://github.com/dmego/qoderbuddy2api/pkgs/container/qoderbuddy2api)
拉取（`ghcr.io/dmego/qoderbuddy2api:latest`）；每次打 tag 发版后自动构建推送。
数据、日志、模型配置全部通过 bind mount 外挂（`./data`、`./logs`、`./config`），
`.env` 原样传入，`restart: unless-stopped` 保证宿主机重启后自动拉起。
具体步骤见 [README.md · Docker deployment](../README.md#docker-deployment)。

## 6. 客户端接入示例

```bash
# OpenAI 兼容
curl http://127.0.0.1:9999/v1/models \
  -H "Authorization: Bearer $QB2API_PROXY_API_KEY"

curl http://127.0.0.1:9999/v1/chat/completions \
  -H "Authorization: Bearer $QB2API_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "你好"}]}'
```

Claude Code / Cursor 等客户端把 base URL 指到 `http://127.0.0.1:9999/v1` 并填入
Proxy Key 即可；管理面与代理面使用不同 key，互不通用。
