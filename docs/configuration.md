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
| `QB2API_CREDENTIAL_REFRESH_ENABLED` | `true` | 短效凭据主动轮换 |
| `QB2API_CREDENTIAL_REFRESH_INTERVAL_SECONDS` | `900` | 轮换扫描间隔（秒） |
| `QB2API_CREDENTIAL_REFRESH_LEAD_SECONDS` | `1800` | 提前多久轮换（秒） |
| `CHECKIN_ENABLED` | `false` | 全局签到调度开关（也可在管理台设置） |
| `CHECKIN_AT` / `CHECKIN_TIMEZONE` | `00:10` / `Asia/Shanghai` | 每日签到时间与时区 |
| `GROWTH_AUTO_ACTIVE_DAY` | `true` | 活跃日自动化开关；由成长调度器每日执行一次正式对话点亮 |

其余签到/指标/用量变量见 `.env.example` 内注释；管理台「设置」页可持久化运行时配置
（签到时间、成长自动化开关、兑换档位等），优先级高于启动默认值。

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
