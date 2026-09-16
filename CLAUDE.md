# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`AGENTS.md` 是本仓库的流程规则（任务粒度、并行 wave、review 与 commit 策略）。

## 常用命令

```sh
go build ./...                      # 编译全部包
go vet ./...                        # 静态检查
go test ./...                       # 全量测试
go test ./internal/store/ -v        # 单包
go test ./internal/server/ -run TestSettingsResponseCarriesSchema -v   # 单个测试
gofmt -w cmd internal               # 格式化（CI 会检查 gofmt -l 为空）

# 本地运行（不打包容器）
QB2API_DATA_DIR=./data QB2API_LOG_DIR=./logs \
  QB2API_CREDENTIAL_KEY=<key> QB2API_ADMIN_KEY=<key> \
  go run ./cmd/qb2api

# 前端：构建产物直接输出到仓库根 web/dist，由 Go 服务托管
cd frontend && npm install --registry=https://registry.npmmirror.com
npm run build          # 产出到 ../web/dist
npm test               # vitest
npx vue-tsc -b         # 类型检查
```

`GOPROXY=https://goproxy.cn,direct`：本机无法访问 proxy.golang.org，拉依赖前先设置。

## 项目是什么

把 WorkBuddy（国内 `codebuddy`）与 WorkBuddy 国际版（`workbuddy_intl`）两个部署的
账号池，包装成 OpenAI 兼容（`/v1/chat/completions`）与 Anthropic 兼容
（`/v1/messages`）的单一入口，并附带一个自托管管理台。

进程模型：**单进程**。Python 版本曾是 Control Plane + Proxy Worker 两个进程，
通过 loopback 上的版本化 JSON 快照握手；Go 版把两个面合成一个二进制。

安全边界不靠进程隔离，而靠类型：代理处理路径只拿到 `*ProxyPlane`，它只暴露
provider 池和模型路由，不持有数据库、Admin Key 或凭据主密钥。凭据只在每次池重建时
解密一次，以不透明的 bearer 串交给 provider。

## 目录

```
cmd/qb2api/           进程入口：生命周期、日志、优雅退出
internal/config/      环境变量配置（变量名与 Python 版完全一致）
internal/vault/       Fernet 兼容的凭据加密（字节级兼容 Python cryptography）
internal/store/       SQLite schema、迁移、各表仓储
internal/models/      模型定义、统一目录、路由策略
internal/chatwire/    OpenAI 兼容的请求/响应类型
internal/providers/   两个上游客户端 + 账号池 + 跨 provider 路由
internal/checkin/     每日签到流水线与调度器
internal/growth/      成长中心自动化与调度器
internal/metrics/     积分/Token 指标采集
internal/oauthflow/   浏览器登录导入流程
internal/importer/    把导入流程产出的账号落库
internal/server/      HTTP 面、管理端鉴权、调度器装配
frontend/             Vue 3 管理台源码（构建到 web/dist）
web/dist/             已构建的管理台产物（随仓库提交，由 Go 服务托管）
go-deploy/            本地部署：compose、切换脚本、运维文档
docs/                 配置与架构文档
```

## 关键不变量

**时间戳格式**：所有入库时间用 `2006-01-02T15:04:05+00:00`（`store.ISOFormat`），
不是 RFC3339——**没有 `Z` 形式**。所有范围过滤都是字符串字典序比较，写成其他形式会静默破坏筛选。

**provider id 不改名**：国内部署的 provider id 是 `codebuddy`，账号、凭据、用途、
路由策略和历史遥测全部以它为键。改名会孤立所有既有数据。

**凭据主密钥不可换**：`QB2API_CREDENTIAL_KEY` 决定已存凭据能否解密；换掉等于全部重新登录。

**上游 HTTP 客户端必须 `Proxy: nil`**：本机跑 TUN 模式代理，httpx/Go 默认读取代理环境变量，
复用半死的池化连接会造成数分钟卡顿（Python 版实测 200+ 秒）。

**流式响应只在第一个下游 chunk 之前允许 failover**。输出之后失败必须原样抛给客户端，
不能跨账号重试——否则用户会看到拼接两次的输出。

**两个百分位公式不能统一**：`/usage/summary` 用 `ceil(n*f)-1`，rollup 聚合用
`round((n-1)*f)` 且 Python 的 round 是银行家舍入。n=12..19 与 31..39 时两者结果不同，
统一会静默改变控制台已展示的数字。见 `internal/store/telemetry.go`。

**遥测不得影响代理**：事件写入是批处理的，且失败只计数不重试，绝不阻塞或失败请求。

## 安全边界（不可降低）

- Control Plane 与代理路径的凭据边界必须清晰：代理面不读 SQLite、不读 Admin Key、
  不读凭据主密钥。
- 原始 Token、Cookie、API Key、Authorization、上游响应正文、prompt/completion
  不得进入日志、审计、SQLite 明文列或前端持久化存储。
- 管理端会话只存哈希；CSRF token 由 session id 确定性派生（`internal/server/admin_auth.go`），
  这样重启后既有会话仍可保存，且校验不会因派生方式变化而漂移。
- 前端是功能完整的管理台，不是演示页。

## 测试约定

- 测试锁**可观测契约**，不锁实现：断言响应形状、状态码、边界、优先级、真实错误，
  不断言 wiring、字段拷贝、默认值转发、mock 回声。
- 回归测试要能在修复前失败（`git stash` 验证 RED，恢复后 GREEN）。
- 需要真实数据库的测试用环境变量开关，默认跳过：
  `QB2API_TEST_DB=<库副本> QB2API_TEST_VAULT_KEY=<key> go test ./internal/store/ -run TestOpensExistingDatabase -v`

## 部署

```sh
cd go-deploy
./switch-to-go.sh --dry-run   # 预演，不改动任何东西
./switch-to-go.sh             # 交互确认后切换
```

生产运行在 9999。详见 `go-deploy/README.md`。
