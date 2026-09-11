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
| `QB2API_MODE` | `control` | 常驻进程模式；不要为 Worker 单独建服务 |
| `QB2API_CONTROL_HOST` / `PORT` | `127.0.0.1` / `9999` | Control Plane 监听地址 |
| `QB2API_WORKER_HOST` / `PORT` | `127.0.0.1` / `10001` | Proxy Worker 监听地址（仅 loopback） |
| `QB2API_WORKER_AUTOSTART` | `true` | Control 启动时自动拉起 Worker |
| `QB2API_WORKER_INTERNAL_TOKEN` | 自动生成 | 留空会在 `data/worker.internal`（0600）生成；不能复用其他 key |
| `QB2API_DATA_DIR` / `LOG_DIR` | `./data` / `./logs` | SQLite、备份、日志目录（运行用户独占） |
| `QB2API_MODEL_CONFIG` | `./config/models.json` | 模型配置路径 |

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
| `CODEBUDDY_TOKEN` / `QODER_TOKEN` / `WORKBUDDY_INTL_TOKEN` / `ORCATERM_TOKEN` | 空 | 旧式静态 token（transient chat slot）；长期账号请在管理台导入 |
| `WORKBUDDY_INTL_ENDPOINT` | `https://www.workbuddy.ai` | WorkBuddy 国际版入口 |
| `WORKBUDDY_INTL_OAUTH_ENABLED` | `true` | 是否允许管理台发起国际版浏览器登录 |
| `WORKBUDDY_INTL_CREDITS_PATH` | `/billing/meter/get-user-resource` | 国际版积分查询路径 |
| `QB2API_INTL_DEFAULT_REASONING_EFFORT` | `low` | 客户端未传 `reasoning_effort` 时注入的档位 |
| `ORCATERM_ENDPOINT` | `https://lightai.cloud.tencent.com` | OrcaTerm lightai 后端入口 |
| `ORCATERM_USER_ID` | 空 | 会话 ID 使用的上游用户号（token 内的 `userId`） |
| `ORCATERM_TIMEOUT` | `300` | OrcaTerm 请求超时（秒） |
| `QB2API_MODEL_SYNC_ENABLED` | `true` | Qoder 上游模型目录自动同步 |
| `QB2API_MODEL_SYNC_INTERVAL_SECONDS` | `21600` | 同步间隔（秒） |
| `QB2API_CREDENTIAL_REFRESH_ENABLED` | `true` | 短效凭据（OrcaTerm）主动轮换 |
| `QB2API_CREDENTIAL_REFRESH_INTERVAL_SECONDS` | `900` | 轮换扫描间隔（秒） |
| `QB2API_CREDENTIAL_REFRESH_LEAD_SECONDS` | `1800` | 提前多久轮换（秒） |
| `CHECKIN_ENABLED` | `false` | 全局签到调度开关（也可在管理台设置） |
| `CHECKIN_AT` / `CHECKIN_TIMEZONE` | `00:10` / `Asia/Shanghai` | 每日签到时间与时区 |
| `GROWTH_AUTO_ACTIVE_DAY_RECHECKIN` | `true` | 活跃日未点亮时先补一次签到再走 ACP；已签到则跳过。按本地日期格子而非官方 `today` 判断，避免 UTC 日界线误判 |

其余签到/指标/用量变量见 `.env.example` 内注释；管理台「设置」页可持久化运行时配置
（签到时间、成长自动化开关、兑换档位等），优先级高于启动默认值。

### WorkBuddy 国际版（www.workbuddy.ai）

国际版与国内版共用 `/v2/chat/completions` 协议，但属于**独立提供商** `workbuddy_intl`：
独立域名、独立登录、独立积分接口，并且**没有签到与成长中心**（因此它的账号只有
`chat` 用途，不会出现在签到/成长页面）。

- **免费额度只覆盖三个模型**：`hy4-preview`、`hy3`、`deepseek-v4.1-flash`。
  其余模型既不可用也不做上游探测，`config/models.json` 的 `workbuddy_intl` 段
  即为唯一事实源；管理台「从上游同步」对国际版不生效。
- **登录**：管理台「添加账号 → WorkBuddy 国际版」发起 plugin OAuth，在浏览器完成登录后
  自动回收凭据；也可直接粘贴 Bearer Token 手动导入。访问令牌有效期约一年，
  到期前 Control Plane 会用 `X-Refresh-Token` 自动轮换。
- **积分监控**：与国内版同一套 `/billing/meter/get-user-resource` 响应结构，
  按账号写入 `points` 快照，在「积分」页与国内账号一起展示。
- 国际版首个上游消息**必须是 system**，且拒绝 OpenAI 的 `developer` 角色；
  代理层会自动补一条 system 消息并把 `developer` 折叠为 `system`。

### OrcaTerm（腾讯云 lightai agent 后端）

OrcaTerm 是**独立提供商** `orcaterm`，与腾讯云 OrcaTerm 桌面版 AI 助手共用后端
（`https://lightai.cloud.tencent.com`），同样是**聊天专用**：账号只有 `chat` 用途，
没有签到与成长中心。

- **免费额度覆盖八个模型**：`hy4-preview`、`hy3`、`kimi-k3`、`glm-5.3`、
  `glm-5.3-flash`、`glm-5.2`、`deepseek-v4-flash`、`deepseek-v4-pro`。
  上游模型 ID 为 `<Provider>/<model>` 形式（如 `TokenHub/glm-5.3`、
  `Hunyuan3/hy4-preview`），由 `config/models.json` 的 `metadata.upstream_id`
  声明，管理台与 `/v1/models` 只暴露统一的短 ID。
- **登录**：管理台「添加账号 → OrcaTerm」点「浏览器登录」，在腾讯云完成登录即可——
  Control Plane 用发起流程时生成的会话 id 轮询 `OAuthExchangeToken` 换取凭据，所以
  发起页要保持打开（导入完成后可关闭）。授权 URL 必须带 `source=desktop`：控制台只在
  desktop 通道把登录绑定到会话 id，web 通道既不生成也不回传该 id，会导致轮询永远停在
  未完成状态。也可展开「手动输入 Bearer Token」粘贴桌面版
  OrcaTerm 的 OAuth Token（桌面 App 数据目录
  `~/Library/Application Support/com.orcaterm-desktop.app/data.bin` 里的
  `oauth_access_token`），或用 `ORCATERM_TOKEN` 环境变量注入。
  **Token 约 2 小时过期**，但 Control Plane 会主动轮换：OrcaTerm 的
  `OAuthRefreshToken` 用当前 access token 自身换新 token（没有独立的 refresh
  token），因此只要在过期前刷新就能一直续下去，账号 ID 不变。调度器每 15 分钟扫描
  一次，对 30 分钟内到期的凭据强制轮换并 reload Worker，无需人工干预。轮换失败
  （token 已过期，上游返回 `TOKEN_EXPIRED`）才会退化为重新登录/导入。
- **协议差异**：OrcaTerm 不是裸补全接口，而是 agent 接口——代理层会先注册会话
  （`/assistant/conversation`）再流式对话（`/assistant/chat`），并把 agent 返回的
  JSON（`taskCompletion` / `thinking`）拆成标准的 `content` 与 `reasoning_content`。
  对话历史以 `historyMessages` 形式携带，system 消息会被折叠进历史。
- **上游限速**：上游对 `chat-rpm` 有限制，代理层把 429 归类为配额错误并交由
  路由层故障转移；建议不要把 `orcaterm` 单独设为一个模型的唯一路由。

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
未配置策略的模型保持默认行为（同梯队等权重轮询）。策略随运行快照下发到 Worker，
修改后立即生效，无需重启。

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
