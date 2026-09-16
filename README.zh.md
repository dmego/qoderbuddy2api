# qoderbuddy2api

把 WorkBuddy 账号池包装成 **OpenAI 兼容**与 **Anthropic 兼容**的统一推理入口，
并附带一个自托管管理台。

Go 实现，单进程、单二进制。除模型调用外的一切——账号池、凭据轮换、签到、
成长中心、积分采集、用量遥测——都在同一个进程里完成。

[English](README.md) | 中文

## 特性

**代理**
- `/v1/chat/completions`（OpenAI）与 `/v1/messages`（Anthropic），含流式
- 统一模型目录：同一模型跨 provider 合并为一条，请求内部按策略轮询
- 账号级故障转移，仅在第一个下游 chunk 之前发生
- 思考内容透传（`reasoning_content` → Anthropic `thinking` block）
- 上游工具调用与多模态消息透传

**管理台**（`/admin`）
- 账号池：导入、探测、启停、按用途分别配置
- 模型与路由策略：按模型设置 provider 优先级、权重与启停
- 凭据：版本、模式、过期状态与可续期能力，密文永不出库
- 用量：首字耗时与请求耗时分开统计，自适应 ms/s 显示，CSV 导出
- 签到与成长中心：调度、手动执行、批次明细与脱敏结果
- 积分监控、审计、代理密钥、运行设置、服务状态

**自动化**
- 每日签到（含补跑窗口与抖动）
- 成长中心任务/抽奖/旅行/兑换，以及通过正式对话点亮的活跃日
- 凭据主动轮换（短效 token 在到期前刷新）
- 用量聚合与明细保留策略

## 架构

```
client ──▶ :9999 ──┬── /v1/*        代理（OpenAI / Anthropic）
                   ├── /api/admin/* 管理 API
                   └── /admin       管理台（静态资源）
                        │
                        ├── SQLite（账号、凭据密文、遥测、调度状态）
                        └── 上游：copilot.tencent.com / www.workbuddy.ai
```

单进程。Python 版本曾是 Control Plane + Proxy Worker 双进程、通过 loopback 上的
版本化 JSON 快照握手；Go 版合成一个二进制后省掉了握手、快照序列化和第二份解释器。

安全边界靠类型而不是进程隔离：代理处理路径只拿到 `*ProxyPlane`（只暴露 provider 池与
模型路由），拿不到数据库、Admin Key 或凭据主密钥。凭据只在每次池重建时解密一次。

详见 [docs/design/architecture.md](docs/design/architecture.md)。

## 快速开始

```sh
# 需要三个密钥；凭据主密钥生成方式：
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

```sh
cat > .env <<'EOF'
QB2API_PROXY_API_KEY=<客户端用的代理密钥>
QB2API_ADMIN_KEY=<管理台密钥>
QB2API_CREDENTIAL_KEY=<上面的 Fernet key>
QB2API_DATA_DIR=./data
QB2API_LOG_DIR=./logs
QB2API_MODEL_CONFIG=./config/models.json
QB2API_ADMIN_UI_ENABLED=true
QB2API_ADMIN_COOKIE_SECURE=auto
EOF

go run ./cmd/qb2api
```

打开 <http://127.0.0.1:9999/admin>，用 `QB2API_ADMIN_KEY` 登录，在「账号」页导入凭据。

## Docker 部署

```sh
docker compose -f go-deploy/docker-compose.go.yml up -d --build
```

镜像基于 Alpine，单静态二进制，约 37 MB。数据、日志、模型配置以 volume 挂载。

### 从 Python 版本切换

```sh
cd go-deploy
./switch-to-go.sh --dry-run   # 预演：只打印将要执行的动作
./switch-to-go.sh             # 交互确认后执行
./switch-to-go.sh --yes       # 无人值守
```

脚本会停止 Python 服务、把 Go 服务移到 9999、重启并验证（`/health`、`/admin`、
`/v1/models`、账号数，以及一次真实的 `deepseek-v4.1-flash` 流式调用），
任何一步失败会自动回滚。详见 [go-deploy/README.md](go-deploy/README.md)。

**数据可直接沿用**：Go 的 Fernet 实现与 Python `cryptography` 字节兼容，
指向既有 `qb2api.sqlite3` 即可，**不需要重新登录**。

## 客户端接入

```sh
# OpenAI 兼容
curl -N http://127.0.0.1:9999/v1/chat/completions \
  -H "Authorization: Bearer $QB2API_PROXY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4.1-flash","stream":true,
       "messages":[{"role":"system","content":"You are a helpful assistant."},
                   {"role":"user","content":"你好"}]}'
```

```sh
# Anthropic 兼容
curl http://127.0.0.1:9999/v1/messages \
  -H "Authorization: Bearer $QB2API_PROXY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4.1-flash","max_tokens":256,
       "messages":[{"role":"user","content":"你好"}]}'
```

可用模型：`curl http://127.0.0.1:9999/v1/models`。

## 文档

- [配置指南](docs/configuration.md) — 全部环境变量与远程访问配置
- [系统架构](docs/design/architecture.md) — 组件、安全模型、数据模型、路由
- [部署与切换](go-deploy/README.md) — compose、切换脚本、回滚
- [实现说明](docs/implementation-notes.md) — 存储兼容、时间戳格式、写盘策略、百分位口径
- [开发说明](CLAUDE.md) — 常用命令与关键不变量

## 开发

```sh
export GOPROXY=https://goproxy.cn,direct
go vet ./... && go test ./...
cd frontend && npm install --registry=https://registry.npmmirror.com && npm test && npm run build
```

前端构建输出到仓库根 `web/dist`，由 Go 服务同源托管。

## 许可证

[MIT](LICENSE)
