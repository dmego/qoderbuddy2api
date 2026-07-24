# Mac Mini 部署与运维手册

2api 的常驻进程是 Control Plane：它持有管理 UI、SQLite、加密凭据、备份和
Supervisor。Proxy Worker 是其受管子进程，默认仅监听 loopback。不要为 Worker 创建
第二个 launchd/systemd unit，也不要让它直接读取 SQLite、Admin Key 或 credential key。

## 1. 安装前边界

- 使用 Python 3.11+、仅管理员可写的工作目录和权限受限的 `.env`。
- 准备三份互不相同的随机值：Proxy Key、Admin Key、Fernet credential key。
- `QB2API_DATA_DIR`（SQLite、`worker.internal`、`backups/`）与日志目录均应只允许
  运行用户读取；不要放入同步盘或公共下载目录。
- 选择 loopback、显式可信 Tailscale/LAN HTTP、或 HTTPS 反向代理之一。公网暴露不受支持。

不要把任何 raw token、cookie、API key、Authorization、prompt 或 completion 写入 URL、
浏览器存储、Git、launchd plist、systemd unit、截图或普通日志。

## 2. 新安装

```bash
git clone https://github.com/dmego/qoderbuddy2api.git "$HOME/qb2api"
cd "$HOME/qb2api"
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
mkdir -p data logs
chmod 600 .env
chmod 700 data logs
```

在本机生成两个 HTTP key 和一个 Fernet key，填入 `.env`，三者不得相同：

```bash
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'
.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

最小管理模式配置：

```ini
QB2API_CONTROL_HOST=127.0.0.1
QB2API_CONTROL_PORT=9999
QB2API_WORKER_HOST=127.0.0.1
QB2API_WORKER_PORT=10001
QB2API_WORKER_AUTOSTART=true
QB2API_ADMIN_UI_ENABLED=true
QB2API_ADMIN_KEY=<different-admin-key>
QB2API_PROXY_API_KEY=<different-proxy-key>
QB2API_CREDENTIAL_KEY=<fernet-key>
QB2API_DATA_DIR=./data
QB2API_LOG_DIR=./logs
QB2API_ADMIN_COOKIE_SECURE=auto
```

从仓库根目录启动并确认 Control Plane：

```bash
.venv/bin/qb2api --mode control
curl --fail http://127.0.0.1:9999/health
```

管理台是 `http://127.0.0.1:9999/admin/`；本机模型客户端使用
`http://127.0.0.1:10001/v1`。Worker 的 `/internal/*` 只能由 Control Plane 通过
内部 token 使用，不能对 LAN/Tailscale 暴露，也不能以 Admin/Proxy Key 代替内部 token。

## 3. 远程管理

### 可信 Tailscale/LAN HTTP

HTTPS 仍是推荐方案，但当每一跳都是可信私网且无法提供 TLS 时，下面的显式配置是支持的：

```ini
QB2API_CONTROL_HOST=<本机可信 Tailscale 或 LAN IP>
QB2API_ADMIN_COOKIE_SECURE=false
QB2API_TRUSTED_PROXY_HEADERS=false
```

它让浏览器 Cookie 不带 `Secure`，但仍为 `HttpOnly`、`SameSite=Lax`。HTTP 不会加密
首次登录的 Admin Key，故它绝不可用于公网端口映射、公共 DNS、共享 Wi-Fi 或不受控反向
代理。管理台显示传输风险提示是预期的。

### HTTPS 反向代理

推荐让 Control Plane 继续绑定 `127.0.0.1`，由本机 TLS 终结代理访问它。代理必须只让
自身连接 Control Plane，并覆盖 `X-Forwarded-For`、`X-Forwarded-Proto: https`。确认其
直连对端 CIDR 后才设置：

```ini
QB2API_ADMIN_COOKIE_SECURE=auto
QB2API_TRUSTED_PROXY_HEADERS=true
QB2API_TRUSTED_PROXY_NETWORKS=127.0.0.1/32
```

这个开关不等于信任所有客户端 header。没有确定代理对端时保留 `false`；不要设置为
`0.0.0.0/0` 或整个客户端网络。

## 4. launchd（Mac Mini 推荐）

模板在 [cn.qb2api.control.plist](../../deploy/launchd/cn.qb2api.control.plist)。复制前把
所有 `REPLACE_ME` 替换为实际用户/仓库，并先创建模板中的 `logs` 目录。

```bash
cd "$HOME/qb2api"
cp deploy/launchd/cn.qb2api.control.plist "$HOME/Library/LaunchAgents/cn.qb2api.control.plist"
plutil -lint "$HOME/Library/LaunchAgents/cn.qb2api.control.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/cn.qb2api.control.plist"
launchctl print "gui/$(id -u)/cn.qb2api.control"
```

重启 Control Plane：

```bash
launchctl kickstart -k "gui/$(id -u)/cn.qb2api.control"
```

该重启会正常停止 Worker、启动新的组合并撤销所有 Admin session。需要停用服务时运行
`launchctl bootout "gui/$(id -u)/cn.qb2api.control"`；该操作不会删除 `.env`、SQLite、
备份或日志，数据清理需另行确认。

## 5. 可选 systemd 开发模板

[qb2api-control.service](../../deploy/systemd/qb2api-control.service) 假设源码/venv 在
`/opt/qb2api`，服务用户为 `qb2api`，环境文件为 `/etc/qb2api/qb2api.env`，数据和日志
分别为 `/var/lib/qb2api`、`/var/log/qb2api`。先按实际路径修改模板，再执行：

```bash
sudo install -d -m 700 -o qb2api -g qb2api /var/lib/qb2api /var/log/qb2api
sudo install -d -m 700 -o root -g qb2api /etc/qb2api
sudo install -m 600 -o root -g qb2api /path/to/qb2api.env /etc/qb2api/qb2api.env
sudo install -m 644 deploy/systemd/qb2api-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qb2api-control
sudo systemctl status qb2api-control
```

它是可选的自管 Linux 模板，不替代发行版级安全审核。

## 6. 生命周期、备份与恢复

- 用管理台 Service 页管理 Worker 的 start/stop/restart/reload。Worker 异常后先确认
  `FAILED` 状态，再执行 restart；Supervisor 按 owner、PID、启动时间和进程组校验，
  不应使用按端口 kill 的脚本。
- Control Plane 自身由前台、launchd 或 systemd 管理。重启它时，旧 Worker 会停止，Admin
  session 会失效，均为预期安全语义。
- Audit/Backup 的 restore dry-run 只校验 checksum、`PRAGMA integrity_check` 和当前
  schema version；返回 `offline_restore_required` 表示已通过 dry-run。
- 真正恢复：停止 Control Plane，先备份当前 `qb2api.sqlite3`，再将已验证 backup 覆盖
  活动数据库，使用相同 `QB2API_CREDENTIAL_KEY` 启动并重新登录。丢失该 key 时无法解密
  已存动态凭据。

`QB2API_DATA_DIR` 中的 `qb2api.sqlite3`、`worker.internal`、`backups/` 应保持 owner-only；
自动生成的 `worker.internal` 和 backup 文件应为 `0600`，目录为 `0700`。

## 7. 账号导入与验收

Qoder chat PAT 与签到 access/refresh 是不同凭据。Windows 侧使用
[Qoder exporter](../../tools/qoder-checkin-exporter/README.md) 生成最小临时 JSON；先建立
durable Qoder chat account，再通过受支持的专用 Qoder check-in import 提交，服务端 probe
失败时不能覆盖原凭据。导出文件完成后安全删除。

WorkBuddy 专用 credential import 尚未集成或未通过实际协议验证时，保持该 purpose 未配置
并记录阻塞；不要把 Cookie/Bearer 塞入 `.env`、URL、浏览器存储或通用 credential API。
专用导入器可用后，运行顺序应是：先通过授权的脱敏 probe 确认 bearer/cookie 模式和每日
签到分类；只在 HTTPS 或显式可信 HTTP 的 Admin session 中提交最小字段；由服务端验证
成功后才启用 check-in；立即删除传输副本。不要从浏览器开发者工具、桌面 profile 或历史
请求中批量复制无关 Cookie。

验收前完成：

- [ ] `.env`、data、logs 权限限制到运行用户；三类 key 不同。
- [ ] 远程模式明确为可信 HTTP 或 HTTPS；若信任转发头，CIDR 与 header 覆盖均正确。
- [ ] Worker 没有独立 unit，`/internal/*` 没有网络暴露。
- [ ] Control health、Worker 模型列表、Control restart、Worker restart 均已确认。
- [ ] 已运行 fresh/migrated smoke，已完成备份 restore dry-run。
- [ ] 管理员没有保存 raw key、cookie 或 exporter JSON 的长期副本。
