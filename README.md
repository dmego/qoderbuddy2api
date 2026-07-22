<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11%2B-blue?style=flat-square&logo=python" alt="Python">
  <img src="https://img.shields.io/badge/License-MIT-green?style=flat-square" alt="License">
  <img src="https://img.shields.io/badge/FastAPI-0.115%2B-009688?style=flat-square&logo=fastapi" alt="FastAPI">
</p>

<h1 align="center">qoderbuddy2api</h1>

<p align="center">
  <strong>CodeBuddy & Qoder CN → OpenAI / Anthropic-Compatible API Proxy</strong><br>
  Unlock enterprise LLMs for Claude Code, Codex, and OpenAI or Anthropic-compatible clients.
</p>

---

## Why?

CodeBuddy and Qoder CN provide access to top-tier models (DeepSeek-V4, Qwen3.8, Qwen3.7, Kimi-K2.7-Code, GLM-5.2, MiniMax-M2.7), but their native APIs are not compatible with OpenAI or Anthropic SDKs. **qoderbuddy2api** bridges this gap with a local lightweight proxy.

- 🚀 **Drop-in replacement** — works with Claude Code, Codex, Continue, Aider, and any OpenAI SDK
- 🧩 **Native Anthropic Messages** — `/v1/messages` for Claude-style clients
- 🔧 **Tool calling** — full function calling support for Claude Code agent workflows
- ⚖️ **Load balanced** — round-robin across multiple API keys with automatic failover

## Quick Start

### Local

```bash
pip install qoderbuddy2api
cp .env.example .env   # edit with your API keys
qb2api                 # starts on port 9999
```

### Environment

Create `.env` from the template, then fill in your tokens:

```bash
cp .env.example .env
```

**Where to get tokens:**

- **CodeBuddy** — copy your session token from CodeBuddy account settings. Looks like `ck_xxx…`
- **Qoder CN** — generate a Personal Access Token at [Qoder CN Integrations](https://qoder.com.cn/account/integrations). Looks like `pt_xxx…`

```ini
# Single key
CODEBUDDY_TOKEN=ck_your_key
QODER_TOKEN=pt_your_key

# Multiple keys (round-robin load balancing)
CODEBUDDY_TOKEN=ck_key1,ck_key2,ck_key3
QODER_TOKEN=pt_key1,pt_key2,pt_key3

# Optional: protect the API with a bearer token
QB2API_API_KEY=your-secret
```

## Usage

### With Claude Code

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

### Direct API

```bash
# List models
curl http://localhost:9999/v1/models

# Streaming chat
curl -N http://localhost:9999/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"codebuddy/deepseek-v3","messages":[{"role":"user","content":"Hello!"}],"stream":true}'

# Tool calling (non-streaming)
curl http://localhost:9999/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qoder/auto","messages":[{"role":"user","content":"Tokyo weather?"}],"tools":[{"type":"function","function":{"name":"get_weather","parameters":{"type":"object","properties":{"city":{"type":"string"}}}}}],"tool_choice":"auto"}'

# Anthropic Messages API
curl http://localhost:9999/v1/messages \
  -H "Content-Type: application/json" \
  -d '{"model":"codebuddy/deepseek-v4-flash","max_tokens":512,"messages":[{"role":"user","content":"Hello!"}]}'
```

## Supported Models

### CodeBuddy (14 models)

| Model | Capabilities |
|-------|-------------|
| `codebuddy/auto` | Auto-routing |
| `codebuddy/deepseek-v3` | Chat, streaming, **tool calling** |
| `codebuddy/deepseek-v4-pro` | Chat, streaming |
| `codebuddy/deepseek-v4-flash` | Chat, streaming |
| `codebuddy/deepseek-r1` | Chat, **reasoning** |
| `codebuddy/glm-5.1`, `glm-5.2`, `glm-5v-turbo` | Chat, streaming |
| `codebuddy/kimi-k2.6`, `kimi-k2.7` | Chat (k2.7: reasoning) |
| `codebuddy/minimax-m3`, `minimax-m2.7` | Chat (m2.7: reasoning) |
| `codebuddy/hy3` | Chat |
| `codebuddy/deepseek-v3-0324` | Chat |

### Qoder CN (10 models)

| Model | Capabilities |
|-------|-------------|
| `qoder/auto` | Auto-routing, **tool calling**, reasoning effort, context window |
| `qoder/Qwen3.8-Max-Preview` | **Tool calling**, reasoning effort, context window |
| `qoder/Qwen3.7-Max` | **Tool calling**, reasoning effort, context window |
| `qoder/DeepSeek-V4-Pro` | **Tool calling**, context window |
| `qoder/Kimi-K2.7-Code` | **Tool calling**, context window |
| `qoder/Qwen3.7-Plus` | Chat, context window |
| `qoder/Qwen3.6-Flash` | Chat, context window |
| `qoder/DeepSeek-V4-Flash` | Chat, context window |
| `qoder/GLM-5.2` | Chat, context window |
| `qoder/MiniMax-M2.7` | Chat, context window |

Prefix with `codebuddy/` or `qoder/` for explicit routing, or use bare model name for auto-discovery.

## Architecture

```
Client (Claude Code / OpenAI SDK)
        │
        ▼
   qoderbuddy2api (FastAPI)
        │
   ┌────┴────────────┐
   │                 │
   ▼                 ▼
CodeBuddy          Qoder CN
(HTTPS)           (COSY Protocol)
```

## Advanced

### Load Balancing

Comma-separate multiple tokens to enable round-robin:

```ini
QODER_TOKEN=pt_account1,pt_account2,pt_account3
```

Failed instances are cooled down for 30 seconds before retry.

### API Key Protection

```ini
QB2API_API_KEY=your-secret
```

With a key configured, all non-health endpoints require `Authorization: Bearer <key>`.

### Service Management

```bash
./server.sh start     # start in foreground
./server.sh stop      # stop by port
./server.sh status    # show status
```

## License

MIT — see [LICENSE](LICENSE) for details.

---

<p align="center">
  <sub>Built for developers who want more model freedom.</sub>
</p>
