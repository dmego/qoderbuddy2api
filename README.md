<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11%2B-blue?style=flat-square&logo=python" alt="Python">
  <img src="https://img.shields.io/badge/License-MIT-green?style=flat-square" alt="License">
  <img src="https://img.shields.io/badge/FastAPI-0.115%2B-009688?style=flat-square&logo=fastapi" alt="FastAPI">
  <img src="https://img.shields.io/badge/Docker-Supported-2496ED?style=flat-square&logo=docker" alt="Docker">
</p>

<h1 align="center">qoderbuddy2api</h1>

<p align="center">
  <strong>CodeBuddy & Qoder CN → OpenAI-Compatible API Proxy</strong><br>
  Unlock enterprise LLMs for Claude Code, Codex, and any OpenAI-compatible client.
</p>

---

## Why?

CodeBuddy and Qoder CN provide access to top-tier models (DeepSeek-V4, Qwen3.7, Kimi-K2.6, GLM-5.2, MiniMax-M2.7), but their native APIs aren't OpenAI-compatible. **qoderbuddy2api** bridges this gap — one command, instant proxy.

- 🚀 **Drop-in replacement** — works with Claude Code, Codex, Continue, Aider, and any OpenAI SDK
- 🔧 **Tool calling** — full function calling support for Claude Code agent workflows
- ⚖️ **Load balanced** — round-robin across multiple API keys with automatic failover
- 🐳 **Docker-ready** — deploy anywhere in 30 seconds
- 🔒 **Zero data leakage** — everything runs locally, no third-party cloud dependencies

## Quick Start

### Docker (Recommended)

```bash
docker compose up -d
```

### Local

```bash
pip install qoderbuddy2api
cp .env.example .env   # edit with your API keys
qb2api                 # starts on port 9999
```

### Environment

```ini
# Single key
CODEBUDDY_TOKEN=ck_your_key
QODER_TOKEN=pt_your_key

# Multiple keys (load-balanced round-robin)
CODEBUDDY_TOKEN=ck_key1,ck_key2,ck_key3
QODER_TOKEN=pt_key1,pt_key2,pt_key3

# Optional: protect with a bearer token
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
| `codebuddy/hy3-preview` | Chat |
| `codebuddy/deepseek-v3-0324` | Chat |

### Qoder CN (9 models)

| Model | Capabilities |
|-------|-------------|
| `qoder/auto` | Auto-routing, **tool calling**, reasoning effort, context window |
| `qoder/Qwen3.7-Max` | **Tool calling**, reasoning effort, context window |
| `qoder/DeepSeek-V4-Pro` | **Tool calling**, context window |
| `qoder/Kimi-K2.6` | **Tool calling**, context window |
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

### Response Cache

Non-streaming chat completions are cached (LRU, TTL 300s by default). Identical requests return instantly:

```bash
# First call hits upstream
curl http://localhost:9999/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"codebuddy/deepseek-v3","messages":[{"role":"user","content":"What is 2+2?"}]}'
# → ~1.2s

# Second identical call returns from cache
# → <0.01s (logged as "Cache HIT")
```

Configure via environment:

```ini
QB2API_CACHE_ENABLED=true    # default: true
QB2API_CACHE_MAX_SIZE=200   # max entries
QB2API_CACHE_TTL=300        # seconds
```

## Roadmap

- [ ] Anthropic Messages API (`/v1/messages`) — Claude Code native support
- [ ] Rate limiting per key
- [ ] Prometheus metrics endpoint
- [ ] Additional providers (open for contributions)

## License

MIT — see [LICENSE](LICENSE) for details.

---

<p align="center">
  <sub>Built for developers who want more model freedom.</sub>
</p>
