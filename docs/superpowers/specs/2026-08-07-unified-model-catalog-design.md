# 统一模型目录设计（Unified Model Catalog）

日期：2026-08-07
状态：已实施（2026-08-07）
范围：Proxy Worker 对外模型列表与模型路由

## 1. 背景与问题

当前 `codebuddy`（workbuddy）与 `qoder` 两套上游各自维护独立的模型列表，对外暴露时存在三个问题：

1. **同一模型重复暴露**：`deepseek-v4-pro` / `deepseek-v4-flash` / `glm-5.2` / `minimax-m2.7` / `auto` 两个提供商都有，但 `/v1/models` 会返回两条，客户端只能二选一。
2. **模型 ID 不规范**：`/v1/models` 返回 `codebuddy/glm-5.2`、`qoder/DeepSeek-V4-Flash` 这类带前缀、大小写混杂的 ID；裸 ID（无前缀）遇到双端共有的模型时直接 400 `Ambiguous`（见 `src/qb2api/worker/proxy_state.py:_resolve_unprefixed`）。
3. **无跨提供商能力**：一个模型只能绑定单一提供商，流量无法在两个提供商之间分摊。

## 2. 目标

- 对外只暴露**一套统一模型列表**，模型 ID 为规范的小写形式（如 `deepseek-v4-flash`、`glm-5.2`），不再带 `provider/` 前缀、不再出现 `DeepSeek-V4-Flash` 这类大小写混杂 ID。
- 双端共有的模型：只暴露一个条目，请求在内部按提供商**轮询（round-robin）+ 首个下游 chunk 前故障转移**，客户端无感。
- 单端独有的模型：照常暴露，但请求只会路由到对应提供商，绝不误发到另一端。
- 兼容性：旧的 `provider/model` 前缀 ID 与旧的裸上游 ID 仍可解析（标记为 deprecated），避免破坏已有客户端。

## 3. 现状梳理（已核实代码路径）

| 环节 | 位置 | 现状 |
|---|---|---|
| 模型定义 | `config/models.json` | 两级结构 `{codebuddy: {models: [...]}, qoder: {models: [...]}}`；codebuddy 用全小写 ID，qoder 用 `DeepSeek-V4-Flash` 式 CamelCase |
| 配置加载 | `src/qb2api/models.py:load_models_from_config` | 按 provider key 遍历，产出 `dict[provider, list[ModelDefinition]]` |
| qoder 上游目录同步 | `src/qb2api/control/runtime_snapshot.py:_upstream_catalog_models` | DB `model_catalog` 表（source=upstream、带 `cosy_key` metadata）合并进 snapshot 的 qoder 段，按 ID 覆盖配置 |
| snapshot 协议 | `src/qb2api/runtime_snapshot.py` | `models: dict[str, tuple[ModelDefinition]]`，按提供商分组，协议版本 v2 |
| 模型索引 | `src/qb2api/worker/proxy_state.py:_build_model_index` | `model_index: dict[provider, set[id]]`，裸 ID 多提供商命中即 400 |
| 解析入口 | `proxy_state.resolve_model(model)` | 返回 `(provider_name, upstream_model_id)`；两条协议路由（openai/anthropic）先解析再查 registry |
| 提供商池 | `src/qb2api/worker/runtime.py` | 每提供商一个 `DynamicProviderPool`：账号级 RR、pre-commit 故障转移、30s 冷却 |
| 上游 ID 映射 | `src/qb2api/providers/qoder_payload.py:qoder_model_key` | qoder ID → COSY 内部 key（`QODER_CLI_MODEL_KEYS` 静态表 + snapshot metadata `cosy_key` 运行时覆盖） |
| 对外列表 | `src/qb2api/worker/metadata_routes.py` | `/v1/models` 输出 `f"{provider}/{model.id}"`；`/v1/props` 按提供商分组输出 |

关键结论：

- 上游侧 ID 必须保留原样：codebuddy 直接把 `request.model` 作为上游 model 字段；qoder 需要自己的 CamelCase ID 才能查到 cosy_key。**统一的是对外 ID，不是上游 ID**。
- Control Plane 与 snapshot 协议**不需要改动**：worker 端可以从按提供商分组的 snapshot 数据推导统一目录。
- 请求对象的 `ChatCompletionRequest.model` 是普通可变字段，可在路由层按路线改写。

## 4. 设计

### 4.1 统一目录构建（Worker 端，无协议变更）

新增模块 `src/qb2api/models_catalog.py`，纯函数、无 IO：

```
UnifiedModel:
  id: str                      # 规范 ID（小写）
  name: str                    # 展示名
  capabilities: ModelCapabilities  # 各路线能力并集
  max_context: int             # 取各路线最大值
  max_output: int              # 取各路线最大值
  routes: tuple[ModelRoute, ...]   # 至少 1 条
  aliases: tuple[str, ...]     # 可选额外公开 ID

ModelRoute:
  provider: str                # "codebuddy" | "qoder"
  upstream_id: str             # 该提供商的原始 ID（如 "DeepSeek-V4-Flash"）
```

构建规则（`build_unified_catalog(per_provider, overrides)`）：

1. **ID 规范化**：`normalize(id) = id.lower()`（去掉首尾空白）。`DeepSeek-V4-Flash` → `deepseek-v4-flash`，`Qwen3.8-Max-Preview` → `qwen3.8-max-preview`。
2. **自动合并**：规范化 ID 相同的跨提供商条目合并为一个 `UnifiedModel`，routes 按提供商排列（codebuddy 在前）。codebuddy 的 ID 本就是规范小写，因此合并后的规范 ID 默认等于 codebuddy 侧 ID。
3. **能力取并集**：`capabilities` 逐字段 OR；`max_context`/`max_output` 取最大值；展示名优先取 codebuddy 侧，否则取 qoder 侧。
4. **显式覆盖**：`config/models.json` 新增可选顶层 `unified` 段，可覆盖规范 ID、展示名、routes（增删路线）、capabilities，并可声明 `aliases`（额外公开 ID）。未配置时全部靠自动合并，配置为纯增量覆盖。
5. **单端模型**：自动生成单路线条目，规范 ID = 规范化后的该端 ID（如 `kimi-k2.7-code`、`qwen3.7-max`）。

`load_models_from_config` 增加保护：只处理已知提供商 key（`codebuddy`/`qoder`），忽略 `unified` 等非提供商段，避免其漏进 snapshot 模型分组。

### 4.2 模型路由（跨提供商轮询 + 故障转移）

新增 `src/qb2api/worker/model_router.py`：

- `ModelRouter` 实现 `Provider` 接口（`complete`/`stream`/`close`），按 `request.model`（规范 ID）查路由表。
- 每个规范 ID 维护 `(cursor, 冷却表)`：轮询起始路线 = `cursor % len(routes)`，每次请求后推进。
- **可用性过滤**：路线对应的提供商池（`DynamicProviderPool`）无可用账号时跳过该路线。
- **pre-commit 故障转移**：沿用仓库既有规则（流式只允许在首个下游 chunk 前 failover）。判据复用请求对象自身状态——`request.telemetry["stream_committed"]`；未提交时失败则标记该路线冷却（30s，与池语义一致）并尝试下一条路线；已提交后任何失败立即上抛，绝不跨提供商重试。
- **上游 ID 改写**：委托前 `request.model = route.upstream_id`，`finally` 恢复为规范 ID（请求对象单飞，无并发共享；遥测与请求日志读到的是恢复后的规范 ID）。
- 单路线模型直接透传对应池，不加层。

接入方式（`resolve_model` 返回类型从二元组改为 `ResolvedModel`，两条协议路由同步适配；对外行为兼容旧 ID 形式）：

```python
@dataclass
class ResolvedModel:
    canonical_id: str        # 规范 ID（遥测/日志使用）
    provider: Provider       # 单路线 = 原池；多路线 = ModelRouter
    upstream_model: str      # 单路线 = 上游 ID；多路线 = 规范 ID（由 router 改写）
```

- 旧 `provider/model` 前缀 ID（`codebuddy/deepseek-v4-flash`、`qoder/DeepSeek-V4-Flash`）：按路线反查统一模型，解析到同一目标，标记 deprecated（仅接受，不再出现在 `/v1/models`）。
- 旧裸上游 ID（`DeepSeek-V4-Flash`）：按路线上游 ID 反查，同样兼容。
- 未知模型：400 + 可用列表（保持现有行为与错误文案风格）。

### 4.3 对外 API 变化

- `/v1/models`：只输出统一 ID（`ModelInfo.id = canonical_id`，`owned_by = "qoderbuddy2api"`）。双端共有模型只出现一次。
- `/v1/props`：`models` 字段改为统一 ID 数组（不再按提供商分组）。
- `/v1/models/{id}`：保持不变（回显）。
- `registry.providers` / `/health`：仍只列 `codebuddy`、`qoder`，不引入虚拟提供商名。

### 4.4 遥测与日志

- 遥测 `model_id` 字段记录**规范 ID**（provider 字段仍记录实际提供商，account_id 仍为实际账号），跨提供商聚合统计口径一致。
- 请求日志 `model` 记录规范 ID。

### 4.5 明确不做（边界）

- **会话保持（session affinity）不做**：代理无会话态，请求无会话标识；qoder 的 COSY session 已在提供商层复用，会话连续性由客户端重发完整上下文保证。轮询 + pre-commit 故障转移已满足需求且无状态、可测试。若未来需要，可按 `user` 字段或请求 hash 做粘性路由，作为独立任务。
- **Control Plane / 管理台模型页（`admin/catalog_routes.py`、前端 ModelsPage）不改**：它们管理的是按提供商的运维视图（同步/probe/启停），是统一目录的数据源之一，保持原样。
- **快照协议（v2）与 DB 结构不变**；无迁移。

## 5. 落地后的统一目录（按当前 config 推演）

双端共有（5 个，单条目双路线，RR + 故障转移）：

| 规范 ID | 展示名 | codebuddy 路线 | qoder 路线 |
|---|---|---|---|
| `auto` | Auto | `auto` | `auto` |
| `deepseek-v4-pro` | DeepSeek V4 Pro | `deepseek-v4-pro` | `DeepSeek-V4-Pro` |
| `deepseek-v4-flash` | DeepSeek V4 Flash | `deepseek-v4-flash` | `DeepSeek-V4-Flash` |
| `glm-5.2` | GLM-5.2 | `glm-5.2` | `GLM-5.2` |
| `minimax-m2.7` | MiniMax M2.7 | `minimax-m2.7` | `MiniMax-M2.7` |

codebuddy 独有（9 个）：`deepseek-v3`、`deepseek-v3-0324`、`deepseek-r1`、`glm-5.1`、`glm-5v-turbo`、`minimax-m3`、`kimi-k2.6`、`kimi-k2.7`、`hy3`

qoder 独有（5 个）：`qwen3.8-max-preview`、`qwen3.7-max`、`qwen3.7-plus`、`qwen3.6-flash`、`kimi-k2.7-code`（与 codebuddy 的 `kimi-k2.7` 规范化后不同，判定为不同模型，保持分离）

合计 19 个对外 ID（原 24 个带前缀条目）。qoder 上游目录后续同步出的新模型自动进入目录（单路线、规范小写 ID），与 codebuddy 侧同模型再次出现时自动合并。

## 6. 文件改动清单

| 文件 | 改动 |
|---|---|
| `src/qb2api/models_catalog.py` | **新增**：规范化、UnifiedModel/ModelRoute、build_unified_catalog、覆盖合并 |
| `src/qb2api/worker/model_router.py` | **新增**：ModelRouter（轮询、冷却、pre-commit 跨提供商故障转移、上游 ID 改写） |
| `src/qb2api/models.py` | `load_models_from_config` 只处理已知提供商；新增 `load_unified_overrides` |
| `src/qb2api/worker/proxy_state.py` | 构建统一目录 + router；`resolve_model` 返回 `ResolvedModel`（兼容旧前缀与裸上游 ID）；`available_models` 改为统一目录（按路线可用性过滤） |
| `src/qb2api/worker/metadata_routes.py` | `/v1/models`、`/v1/props` 输出统一 ID |
| `src/qb2api/worker/openai_routes.py`、`anthropic_routes.py` | 适配 `ResolvedModel`（provider 直接调用，不再 `registry.get` 字符串） |
| `config/models.json` | 新增可选 `unified` 覆盖段（当前可为空） |
| `tests/` | 见下节 |

不改：`runtime_snapshot.py`（协议）、`worker/runtime.py`（池）、`providers/*`（上游实现）、DB schema、前端、`.env`。

## 7. 测试与验证

- **单测（新增）**：
  - 目录构建：规范化（`DeepSeek-V4-Flash`→`deepseek-v4-flash`）、自动合并、能力并集、单端模型、覆盖段优先级、`kimi-k2.7` vs `kimi-k2.7-code` 不误合并。
  - 路由：轮询起始点推进、路线冷却、池无账号时跳过、pre-commit 失败转移到另一提供商、已提交后失败不重试、单路线透传。
  - 兼容解析：`codebuddy/glm-5.2`、`qoder/DeepSeek-V4-Flash`、裸 `DeepSeek-V4-Flash` 均解析到统一模型；未知模型 400。
- **适配既有测试**：`tests/test_app.py`（`resolve_model` 断言：`auto` 不再 Ambiguous，改为双路线解析）、`tests/integration/test_worker_protocol.py`（`/v1/models` ID 格式断言）。
- **任务级验证**：Python full suite + Ruff；真实双进程 smoke 中 `/v1/models` 应只含 19 个统一 ID；用 `deepseek-v4-flash` 连续请求观察两个提供商轮询（遥测 provider 字段交替），停掉一端账号后请求仍成功（故障转移）。

## 8. 风险与权衡

- **规范化碰撞误合并**：两个确实不同的模型规范化后 ID 相同会被自动合并。缓解：当前目录逐项核对（见第 5 节），显式 `unified` 段可强制拆分（routes 只留一端或改名）；`kimi-k2.7`/`kimi-k2.7-code` 已验证不碰撞。
- **跨提供商轮询使单会话可能落在不同提供商**：模型本身等价（用户前提），qoder COSY session 为提供商级复用，无会话状态丢失；若某提供商账号配额消耗更快，冷却与轮询会自然偏向可用方。
- **旧 ID 兼容是临时性**：文档标记 deprecated，后续可设开关强制拒绝。
- **故障转移语义**：严格遵守仓库既有规则——首个下游 chunk 输出后绝不跨提供商/跨账号重试。
