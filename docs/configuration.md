# 配置指南

本文是 qoderbuddy2api 的配置参考。快速上手见仓库根目录 [README](../README.md) 与
[README.zh](../README.zh.md)；完整部署与运维见 [Mac Mini 部署手册](deployment/macmini.md)。

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

- 不要把 raw key、token、Cookie、Authorization、prompt 或 completion 写入 URL、浏览器
  存储、Git、截图、launchd/systemd 文件或普通日志。
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
| `CODEBUDDY_TOKEN` / `QODER_TOKEN` | 空 | 旧式静态 token（transient chat slot）；长期账号请在管理台导入 |
| `QB2API_MODEL_SYNC_ENABLED` | `true` | Qoder 上游模型目录自动同步 |
| `QB2API_MODEL_SYNC_INTERVAL_SECONDS` | `21600` | 同步间隔（秒） |
| `CHECKIN_ENABLED` | `false` | 全局签到调度开关（也可在管理台设置） |
| `CHECKIN_AT` / `CHECKIN_TIMEZONE` | `00:10` / `Asia/Shanghai` | 每日签到时间与时区 |
| `GROWTH_AUTO_ACTIVE_DAY` | `true` | 登录自动化（每日活跃日）：经成长调度器每天执行一次 WorkBuddy 对话点亮当天 |
| `GROWTH_ACTIVE_DAY_CONFIRM_ATTEMPTS` | `3` | 活跃日上游确认尝试上限，达上限标记 `not_lit` |

其余签到/指标/用量变量见 `.env.example` 内注释；管理台「设置」页可持久化运行时配置
（签到时间、成长自动化开关、兑换档位等），优先级高于启动默认值。

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

## 5. 常驻服务

- **macOS（推荐）**：launchd 模板 `deploy/launchd/cn.qb2api.control.plist`，替换
  `REPLACE_ME` 后使用。步骤见 [部署手册 §4](deployment/macmini.md)。
- **Linux（可选开发）**：`deploy/systemd/qb2api-control.service`，步骤见
  [部署手册 §5](deployment/macmini.md)。

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
