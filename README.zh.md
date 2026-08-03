# qoderbuddy2api

`qoderbuddy2api` 是 CodeBuddy 与 Qoder CN 账号的本地运维平台。常驻 Control
Plane 负责管理台、加密凭据、SQLite、调度、备份和 Worker 监督；Proxy Worker 提供
OpenAI / Anthropic 兼容模型接口。

要求 Python 3.11 及以上。它服务于 Mac Mini 或开发机器上的一位可信运维者，不是公网
多租户网关。

## 拓扑与凭据边界

```text
浏览器（管理） ─────┐
CLI 客户端（/v1）───┴──> Control Plane :9999
                        ├─ 管理台、SQLite、审计、备份、Supervisor
                        └─ /v1/* 转发到 Proxy Worker 127.0.0.1:10001
```

Control Plane 是唯一常驻服务，它启动和管理 Worker；不要为 Worker 单独建立
launchd/systemd 服务。Worker 默认仅监听 loopback，且不直接访问 SQLite，不持有
Admin Key 或凭据加密主密钥。

三类值必须彼此不同：

| 变量 | 作用 |
| --- | --- |
| `QB2API_PROXY_API_KEY` | Worker 的模型客户端请求 |
| `QB2API_ADMIN_KEY` | 首次管理登录和管理员自动化 |
| `QB2API_CREDENTIAL_KEY` | 持久 Provider 凭据的静态加密 |

不要把 key、token、Cookie、Authorization、prompt 或 completion 放进 URL、浏览器
LocalStorage/sessionStorage、Git、截图或普通日志。

## 本地快速开始

```bash
git clone https://github.com/dmego/qoderbuddy2api.git
cd qoderbuddy2api
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
chmod 600 .env
mkdir -p data logs && chmod 700 data logs
```

在本机生成两份独立 HTTP Key 与一份 Fernet 凭据密钥，填入 `.env`：

```bash
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'
.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

启用管理台、持久账号、签到或备份前必须设置 `QB2API_ADMIN_KEY` 与
`QB2API_CREDENTIAL_KEY`。从仓库根目录启动，以读取 `.env`：

```bash
.venv/bin/qb2api --mode control
```

默认地址：

- Control health：`http://127.0.0.1:9999/health`
- 管理台：`http://127.0.0.1:9999/admin/`
- 统一代理入口（推荐）：`http://127.0.0.1:9999/v1`
  - 模型列表：`http://127.0.0.1:9999/v1/models`
  - OpenAI base URL：`http://127.0.0.1:9999/v1`
  - Anthropic Messages：`http://127.0.0.1:9999/v1/messages`
- 直连 Worker（兼容，可选）：`http://127.0.0.1:10001/v1`

客户端只填统一入口 `9999` 即可：Control Plane 会把 `/v1/*` 转发到 Worker。
模型请求通过 `Authorization: Bearer …` 请求头只传递 Proxy Key；管理面与代理面
仍是两个进程，进程级安全边界不变，只是对外呈现单端口。

## 可信远程 HTTP 与 HTTPS

所有非 loopback 浏览器优先使用 HTTPS。默认
`QB2API_ADMIN_COOKIE_SECURE=auto` 允许本机 HTTP，但拒绝远程 HTTP 登录。

如果可信 Tailscale/LAN 暂时无法提供 TLS，可以显式支持 HTTP：

```ini
QB2API_CONTROL_HOST=100.101.102.103
QB2API_CONTROL_PORT=9999
QB2API_ADMIN_COOKIE_SECURE=false
```

这是明确接受传输风险的模式：Cookie 仍是 `HttpOnly` 与 `SameSite=Lax`，但首次提交
Admin Key 没有加密。它只适用于受信 tailnet/LAN，绝不能结合公网 DNS、端口转发、共享
Wi-Fi 或公共反向代理。

HTTPS 模式下让 Control Plane 继续绑定 loopback，并使用 TLS 反向代理。只有明确其直连
对端 CIDR 后，才开启转发头信任：

```ini
QB2API_CONTROL_HOST=127.0.0.1
QB2API_ADMIN_COOKIE_SECURE=auto
QB2API_TRUSTED_PROXY_HEADERS=true
QB2API_TRUSTED_PROXY_NETWORKS=127.0.0.1/32
```

代理必须覆盖 `X-Forwarded-For`、`X-Forwarded-Proto`；不要向任意客户端或宽泛网段开放
该信任。

## 运维、备份与 smoke

Service 页（或 Admin-Key-protected service API）只管理 Worker。Control Plane 必须通过
launchd/systemd 重启。重启 Control Plane 会停止其 Worker，并按设计撤销浏览器 session，
因此需要重新登录。

备份 restore API 仅做校验：检查 checksum、SQLite 完整性和 schema 兼容性，返回
`offline_restore_required`。真实恢复须停止 Control Plane 后再用已验证的 SQLite 备份
覆盖活动数据库。

```bash
PYTHON_BIN=.venv/bin/python bash scripts/smoke_fresh_install.sh
PYTHON_BIN=.venv/bin/python bash scripts/smoke_migrated_install.sh
```

smoke 使用临时数据目录，验证 Control/Worker 启动、Worker 异常后受管重启和备份 dry-run。
除非设置 `QB2API_SMOKE_KEEP=1`，它会删除临时产物。

## 运行手册

- [Mac Mini 部署与运维](docs/deployment/macmini.md)
- [单进程迁移](docs/migration/single-process-to-control-worker.md)
- [launchd Control Plane 模板](deploy/launchd/cn.qb2api.control.plist)
- [可选 systemd 开发模板](deploy/systemd/qb2api-control.service)
- [Qoder Windows 签到导出器](tools/qoder-checkin-exporter/README.md)

环境变量 token 只是 transient chat slot。需要长期身份、签到或凭据轮换时，通过管理台
promote/import。Qoder chat PAT 和 Qoder 签到 access/refresh 是不同值。WorkBuddy 签到
凭据从 `/admin/accounts/add` 的 CodeBuddy / WorkBuddy Check-in 流程导入，支持 Bearer、
Cookie 或 Bearer + Cookie；服务端验证成功或确认当日已签到后才持久化。不要把这些凭据
放进 `.env`、URL、浏览器存储或不支持的通用接口。

## 开发验证

```bash
pytest -q
ruff check src tests
python -m compileall -q src/qb2api
git diff --check
```

## 许可证

MIT — 见 [LICENSE](LICENSE)。
