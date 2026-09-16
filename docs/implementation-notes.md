# 实现说明（Go）

本文记录 Go 实现的内部细节与**必须保持不变的不变量**：存储兼容性、写盘策略、
时间戳格式、延迟统计口径。面向改动这套代码的人；使用与部署见
[README](../README.md) 与 [go-deploy/README.md](../go-deploy/README.md)。

## 为什么重写

Python 版本是 Control Plane + 受管 Proxy Worker 双进程，通过 loopback 上的版本化
JSON 快照握手，各自付一份解释器与依赖。Go 版用一个静态二进制同时提供两个面：

| | Python | Go |
|---|---|---|
| processes | 2 (control + worker) | 1 |
| resident memory | ~360 MB combined | ~30 MB |
| startup | interpreter import + handshake | instant |
| request-path writes | one SQLite transaction + WAL fsync per request | batched |

The security property the two-process split protected — the request path cannot
read SQLite, the admin key, or the credential master key — is preserved
structurally rather than by process isolation: the proxy handlers receive only
`*ProxyPlane`, which exposes provider pools and model routing and holds nothing
else. Credentials are decrypted once per pool reload and reach the providers as
opaque bearer strings.

## 范围

只保留两个 WorkBuddy 部署：

| provider id | 产品 | 上游 |
|---|---|---|
| `codebuddy` | WorkBuddy（国内） | 对话 `copilot.tencent.com`；签到/成长 `www.workbuddy.cn` |
| `workbuddy_intl` | WorkBuddy 国际版 | `www.workbuddy.ai` |

已移除：`qoder` 与 `orcaterm`，以及它们的模型同步调度器、设备 token 派生、
`qoder_checkin_disabled` / `qoder_checkin_reauth_required` 业务码。前端不再提供。

**`codebuddy` 这个 id 是故意不改名的**：账号、凭据、用途、路由策略和全部历史遥测
都以它为键，改名会孤立所有既有数据。

## 目录

```
cmd/qb2api            process entrypoint, lifecycle, logging
internal/config       environment settings (names unchanged from Python)
internal/vault        Fernet-compatible credential encryption
internal/store        SQLite schema, migrations, repositories
internal/models       model definitions, unified catalog, route policy
internal/chatwire     OpenAI-compatible request/response types
internal/providers    upstream WorkBuddy clients, pool, cross-provider router
internal/checkin      daily sign-in pipeline and scheduler
internal/growth       growth-centre automation and scheduler
internal/metrics      credits/token metric collector
internal/oauthflow    browser-login import flows
internal/importer     stores accounts produced by the import flows
internal/server       HTTP surface, admin auth, schedulers wiring
```

## 存储兼容

`internal/store/schema.go` 逐字复刻了 Python 的 DDL —— 相同的表、列、类型与约束名 ——
因此二进制可以直接指向既有 `qb2api.sqlite3`，**没有迁移步骤**。新增列都是追加式的，
由 `Migrate` 应用，对已有数据的库是幂等的。

两处写盘量的改动针对 Python 版实测出的热点：

1. **请求遥测按批写入。** Python 版每个请求一次事务（含 WAL fsync）；Go 的事件写入器
   按 `QB2API_EVENT_FLUSH_MAX`（默认 200）或 `QB2API_EVENT_FLUSH_MILLIS`（默认 250ms）
   合并成一个事务。遥测**绝不允许**阻塞或失败代理请求：失败的批次只计数丢弃，
   不重试成积压。
2. **指标历史不再存逐包明细。** `packages` 占 `points` 载荷约 97% 的字节，而历史趋势
   只读标量总量。`account_metric_snapshots` 保留完整载荷（积分明细页要渲染），
   `account_metric_history` 存去掉 `packages` 的载荷。`main.compactHistory`
   在首次启动时重写 Python 采集器写入的行。

`internal/store/telemetry.go` 还分别移植了 Python 版的**两个百分位公式**：
`/usage/summary` 用 `ceil(n*f)-1`，rollup 聚合用 `round((n-1)*f)` 且 round 是
银行家舍入。两者在某些样本数下结果确实不同（n=12..19 与 31..39），
统一会静默改变控制台已经展示过的数字。

## 时间戳格式

所有入库时间都用 `2006-01-02T15:04:05+00:00`（`store.ISOFormat`），
即 Python `datetime.now(UTC).replace(microsecond=0).isoformat()` 的输出。
这**不是 RFC3339** —— 没有 `Z` 形式。所有范围过滤都是字符串字典序比较，
写成 `Z` 后缀会静默破坏筛选。

## 延迟统计

`request_events.first_token_ms`（新增列）记录**首字耗时**：第一个含内容的 SSE 帧
到达客户端的时刻，与 `latency_ms`（请求总耗时）分开。两者都进用量页，
由自适应格式化函数渲染（`<1s` 用 ms，`>=1s` 用一位小数秒，`>=60s` 用 `m s`），
避免长思考请求打印六位数毫秒。

`started_at` 是**请求到达**的时刻，`finished_at` 是写入遥测的时刻，两者相差
`latency_ms`。控制台按 `started_at` 排序与分桶，所以跨越整分钟的请求不会被记到
它结束的那一分钟里。

## 思考档位（reasoning_effort）优先级

**客户端传了就用客户端的**；只有客户端没传时才注入各 provider 的默认值
（`QB2API_CODEBUDDY_DEFAULT_REASONING_EFFORT`=`max` /
`QB2API_INTL_DEFAULT_REASONING_EFFORT`=`low`）。管理台「思考」列显示的就是这条
规则的结果。

`reasoning_effort` 是**具名结构体字段**，因此 `ChatRequest.UnmarshalJSON` 会把它
从 `Extra` 里删掉。这意味着它必须像 `temperature` 那样在 `requestValues` 里有
**显式 case**：只把它列进 `passthroughKeys` 会走 `ExtraValue` 分支而永远取不到值，
结果是客户端档位被静默丢弃、全部请求回落成 provider 默认值。

## 请求结局（三分支）

`request_events.status` 有三个取值，用量页的成功/已取消/失败都按它统计：

| status | 含义 | 计入 |
|---|---|---|
| `succeeded` | 正常完成 | `success_count` |
| `failed` | 上游或代理自身的故障 | `error_count` |
| `cancelled` | 调用方主动断开或放弃（`context.Canceled`、EPIPE、ECONNRESET） | 仅 `request_count` |

调用方取消既不是成功也不是故障：把它记成失败会虚高错误率，把 `http_status`
一律写成 502 则会让客户端行为看起来像上游故障。因此取消记 `http_status=499`
（nginx 的 client-closed-request 约定），`error_code=client_canceled`。

`error_code` 是**稳定的分类**（`providers.ErrorCode`），不是 Go 类型名：
`upstream_error`、`channel_blocked`、`quota_exceeded`、`no_available_routes`、
`client_canceled`、`upstream_timeout`、`upstream_truncated`、`upstream_eof`、
`deadline_exceeded`、`unknown_error`。之前存的是 `%T`（`*fmt.wrapError`），
把所有失败压成同一个无信息的值。新增成员只增不改名，控制台和 CSV 按它分组。

## 环境变量

变量名与 Python 版完全一致。Go 新增：

| variable | default | purpose |
|---|---|---|
| `QB2API_EVENT_FLUSH_MILLIS` | `250` | telemetry batch window |
| `QB2API_EVENT_FLUSH_MAX` | `200` | events per batch |
| `QB2API_WEB_DIR` | auto | admin console directory override |
| `QB2API_UPSTREAM_HEADER_TIMEOUT_SECONDS` | `300` | 等待上游响应头的上限（不含响应体），`0` 关闭 |

`QB2API_UPSTREAM_HEADER_TIMEOUT_SECONDS` 对应 Python 版的
`httpx.Timeout(300, connect=10)`：它只约束「请求体写完 → 收到响应头」这段，
从不约束响应体，因此慢流式输出不受影响。超时发生在 commit 之前，
所以账号轮询与路由故障转移仍可接管。设置过小会误伤合法的大上下文请求
（250k tokens 的首字实测可到 ~100s），只应当作「上游挂死」的兜底。

`QB2API_CREDENTIAL_KEY` 是必需的：没有它已存凭据无法解密，进程会拒绝启动。

## 开发

```sh
export GOPROXY=https://goproxy.cn,direct   # 本机无法访问 proxy.golang.org
go build ./...
go vet ./...
go test ./...
```

## 配置注意事项

- 上游 HTTP 客户端在 transport 上设 `Proxy: nil`。部署主机跑 TUN 模式代理，
  复用经它建立的半死池化连接曾造成 200+ 秒卡顿；Go 的默认 transport 会继承同样的
  代理环境变量，所以必须显式关闭。
- 单进程同时提供代理与控制台，因此只有 `QB2API_PORT` 决定实际监听端口。
  `QB2API_CONTROL_PORT`/`QB2API_WORKER_PORT` 仍会被读取，让既有 `.env` 无需改动。
