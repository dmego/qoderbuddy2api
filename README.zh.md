<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11%2B-blue?style=flat-square&logo=python" alt="Python">
  <img src="https://img.shields.io/badge/License-MIT-green?style=flat-square" alt="License">
  <img src="https://img.shields.io/badge/FastAPI-0.115%2B-009688?style=flat-square&logo=fastapi" alt="FastAPI">
</p>

<h1 align="center">qoderbuddy2api</h1>

<p align="center">
  <strong>CodeBuddy & Qoder CN → OpenAI / Anthropic 兼容 API 代理</strong><br>
  将企业级大模型解锁给 Claude Code、Codex 及 OpenAI 或 Anthropic 兼容客户端使用。
</p>

---

## 为什么需要它？

CodeBuddy 和 Qoder CN 提供顶尖模型（DeepSeek-V4、Qwen3.8、Qwen3.7、Kimi-K2.7-Code、GLM-5.2、MiniMax-M2.7），但它们原生 API 不兼容 OpenAI 或 Anthropic SDK。**qoderbuddy2api** 通过本地轻量代理补齐这一层。

- 🚀 **即插即用** — 兼容 Claude Code、Codex、Continue、Aider 及任意 OpenAI SDK
- 🧩 **原生 Anthropic Messages** — `/v1/messages` 适配 Claude 风格客户端
- 🔧 **工具调用** — 完整支持 Function Calling，适配 Claude Code 智能体工作流
- ⚖️ **负载均衡** — 多 API Key 轮询 + 自动故障转移

## 快速开始

### 本地部署

```bash
pip install qoderbuddy2api
cp .env.example .env   # 编辑填入 API Key
qb2api                 # 启动在 9999 端口
```

### 环境变量

从模板创建 `.env`，填入你的 token：

```bash
cp .env.example .env
```

**获取 Token：**

- **CodeBuddy** — 在 CodeBuddy 账号设置中复制 session token，格式为 `ck_xxx…`
- **Qoder CN** — 在 [Qoder CN 集成页面](https://qoder.com.cn/account/integrations) 生成个人访问令牌（PAT），格式为 `pt_xxx…`

```ini
# 单 Key
CODEBUDDY_TOKEN=ck_your_key
QODER_TOKEN=pt_your_key

# 多 Key（负载均衡轮询）
CODEBUDDY_TOKEN=ck_key1,ck_key2,ck_key3
QODER_TOKEN=pt_key1,pt_key2,pt_key3

# 可选：用 Bearer Token 保护 API
QB2API_API_KEY=your-secret
```

## 使用方式

### 配合 Claude Code

```json
{
  "providers": {
    "qoderbuddy2api": {
      "baseURL": "http://localhost:9999/v1",
      "apiKey": "optional-if-set"
    }
  }
}
```

### 直接调用 API

```bash
# 列出模型
curl http://localhost:9999/v1/models

# 流式对话
curl -N http://localhost:9999/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"codebuddy/deepseek-v3","messages":[{"role":"user","content":"你好！"}],"stream":true}'

# 工具调用（非流式）
curl http://localhost:9999/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qoder/auto","messages":[{"role":"user","content":"东京天气怎么样？"}],"tools":[{"type":"function","function":{"name":"get_weather","parameters":{"type":"object","properties":{"city":{"type":"string"}}}}}],"tool_choice":"auto"}'

# Anthropic Messages API
curl http://localhost:9999/v1/messages \
  -H "Content-Type: application/json" \
  -d '{"model":"codebuddy/deepseek-v4-flash","max_tokens":512,"messages":[{"role":"user","content":"你好！"}]}'
```

## 支持的模型

### CodeBuddy（14 个模型）

| 模型 | 能力 |
|-------|------|
| `codebuddy/auto` | 自动路由 |
| `codebuddy/deepseek-v3` | 对话、流式、**工具调用** |
| `codebuddy/deepseek-v4-pro` | 对话、流式 |
| `codebuddy/deepseek-v4-flash` | 对话、流式 |
| `codebuddy/deepseek-r1` | 对话、**推理** |
| `codebuddy/glm-5.1`、`glm-5.2`、`glm-5v-turbo` | 对话、流式 |
| `codebuddy/kimi-k2.6`、`kimi-k2.7` | 对话（k2.7：推理） |
| `codebuddy/minimax-m3`、`minimax-m2.7` | 对话（m2.7：推理） |
| `codebuddy/hy3` | 对话 |
| `codebuddy/deepseek-v3-0324` | 对话 |

### Qoder CN（10 个模型）

| 模型 | 能力 |
|-------|------|
| `qoder/auto` | 自动路由、**工具调用**、推理强度、上下文窗口 |
| `qoder/Qwen3.8-Max-Preview` | **工具调用**、推理强度、上下文窗口 |
| `qoder/Qwen3.7-Max` | **工具调用**、推理强度、上下文窗口 |
| `qoder/DeepSeek-V4-Pro` | **工具调用**、上下文窗口 |
| `qoder/Kimi-K2.7-Code` | **工具调用**、上下文窗口 |
| `qoder/Qwen3.7-Plus` | 对话、上下文窗口 |
| `qoder/Qwen3.6-Flash` | 对话、上下文窗口 |
| `qoder/DeepSeek-V4-Flash` | 对话、上下文窗口 |
| `qoder/GLM-5.2` | 对话、上下文窗口 |
| `qoder/MiniMax-M2.7` | 对话、上下文窗口 |

使用 `codebuddy/` 或 `qoder/` 前缀显式路由，或用裸模型名自动发现。

## 架构

```
客户端 (Claude Code / OpenAI SDK)
        │
        ▼
   qoderbuddy2api (FastAPI)
        │
   ┌────┴────────────┐
   │                 │
   ▼                 ▼
CodeBuddy          Qoder CN
(HTTPS)           (COSY 协议)
```

## 高级用法

### 负载均衡

逗号分隔多个 Token 即可启用轮询：

```ini
QODER_TOKEN=pt_账号1,pt_账号2,pt_账号3
```

故障实例被冷却 30 秒后重试。

### API Key 保护

```ini
QB2API_API_KEY=your-secret
```

配置后，除 `/health` 外的所有端点需要 `Authorization: Bearer <key>`。

### 服务管理

```bash
./server.sh start     # 前台启动
./server.sh stop      # 按端口停止
./server.sh status    # 查看状态
```

## License

MIT — 详见 [LICENSE](LICENSE)

---

<p align="center">
  <sub>为追求模型自由的开发者而建。</sub>
</p>
