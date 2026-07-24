# 从单进程迁移到 Control Plane + Proxy Worker

新架构中，Control Plane 持有管理 UI、SQLite、凭据、调度和 Supervisor；Proxy Worker
只负责模型请求。首次启动会做 SQLite 前向 schema migration，但不会自动删除旧 `.env`、
数据库或旧程序。迁移前先建立可恢复副本。

## 1. 端口映射

| 目标 | Control Plane | Proxy Worker | 客户端 |
| --- | --- | --- | --- |
| 保留旧 proxy `9999` | `127.0.0.1:10002` | `127.0.0.1:9999` | 本机 `/v1` 地址不变 |
| 保留旧管理端口 `9999` | `127.0.0.1:9999` | `127.0.0.1:10001` | 代理客户端改到 10001 或经单独反向代理 |

不要让旧进程、新 Control Plane、Worker 占用同一端口。发现冲突时人工确认并停止旧服务；
禁止按端口盲杀。当前 split entrypoint 使用明确的 `QB2API_CONTROL_HOST/PORT` 与
`QB2API_WORKER_HOST/PORT`：旧 `QB2API_HOST/QB2API_PORT` 不会自动重映射为 Worker，
必须按上表更新 `.env`。

## 2. 迁移前

1. 记录旧版本 commit、启动命令、监听端口和客户端 base URL。
2. 停止新请求并等待现有流完成。
3. 在旧服务停止后，复制旧 `.env`、SQLite（如有）、credential key 和旧运行环境说明到
   受保护的离线位置。
4. 不要把旧 `QB2API_API_KEY` 当作 Admin Key。它只是 deprecated Proxy-only alias；生成
   独立 Proxy Key、Admin Key 和 Fernet credential key。
5. 确认备份不在公共 Git、聊天附件、同步盘或浏览器下载目录中。

## 3. 启动迁移

```bash
cp .env.example .env
chmod 600 .env
mkdir -p data logs
chmod 700 data logs
.venv/bin/qb2api --mode control
```

将已有 `QB2API_CREDENTIAL_KEY` 保留给已有加密数据库；新 key 无法解密旧动态凭据。旧环境
变量 token 只作为 transient chat slot 出现，若需要长期身份、签到或轮换，应在管理台中
explicit promote/import。

Control Plane 先连接 SQLite、执行迁移、装配 registry/model snapshot，成功后才启动
Worker。确认 `/health` 为 `component=control-plane`，管理台显示 Worker `HEALTHY` 后，
才让客户端改用 Worker `/v1` 地址和 `Authorization: Bearer` Proxy Key。不要把 key 拼进
base URL。Control Plane 初启会撤销旧浏览器 session，因此重新登录是正常行为。

## 4. 数据、dry-run 与回滚

`QB2API_DATA_DIR` 包含 `qb2api.sqlite3`、`worker.internal`、`backups/`。迁移完成后立即
创建备份并运行 restore dry-run；它验证 checksum、SQLite 完整性和 schema version，
`offline_restore_required` 是预期成功结果。

真正恢复必须停 Control Plane（其 Worker 也会退出），保存当前 SQLite 副本，再复制已验证
backup 到活动 `qb2api.sqlite3`，用相同 credential key 启动并重新登录。

若启动、snapshot 或客户端切换失败：停止新 Control Plane；不要删除新表或备份；使用迁移
前保存的旧程序、`.env`、数据库和端口方案回到单进程。不要让新旧进程同时争抢端口。

## 5. 可执行验证与账号边界

```bash
PYTHON_BIN=.venv/bin/python bash scripts/smoke_fresh_install.sh
PYTHON_BIN=.venv/bin/python bash scripts/smoke_migrated_install.sh
```

第二个脚本用临时 v3 SQLite fixture 验证当前 schema migration、旧账号元数据保留与 Control
restart，不接触生产数据。

Qoder chat PAT 与 Qoder check-in access/refresh 必须分开导入；Windows 的最小导出流程见
[exporter](../../tools/qoder-checkin-exporter/README.md)。没有经过验证的 WorkBuddy 专用
import workflow 时，不要把 Cookie/Bearer 保存进 `.env`、URL、浏览器存储或不支持的通用
凭据接口。

迁移完成检查：

- [ ] 已保存旧 `.env`、SQLite、credential key 和端口映射。
- [ ] 新旧端口没有冲突；Control/Worker 都健康且 Worker 仍 loopback-only。
- [ ] Key 已分离，未把 `QB2API_API_KEY` 用作 Admin Key。
- [ ] migrated smoke、备份 dry-run 和一条受控模型请求均已完成。
- [ ] 浏览器 session 已重新建立，未复制或保存 session cookie。
