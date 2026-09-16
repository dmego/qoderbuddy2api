# qoderbuddy2api

Turn a pool of WorkBuddy accounts into a single **OpenAI-compatible** and
**Anthropic-compatible** inference endpoint, with a self-hosted admin console
attached.

Go implementation: one process, one static binary. Everything except the model
call itself — account pool, credential rotation, daily sign-in, growth-centre
automation, credits collection, usage telemetry — happens in that process.

English | [中文](README.zh.md)

## Features

**Proxy**
- `/v1/chat/completions` (OpenAI) and `/v1/messages` (Anthropic), streaming included
- Unified model catalog: one model served by several providers collapses to a
  single id, routed internally by policy
- Account-level failover, only before the first downstream chunk
- Reasoning passthrough (`reasoning_content` → Anthropic `thinking` block)
- Upstream tool calls and multimodal messages passed through

**Admin console** (`/admin`)
- Accounts: import, probe, enable/disable, per-purpose configuration
- Models and routing policy: per-model provider priority, weight and enablement
- Credentials: version, mode, expiry state and renewability; ciphertext never leaves the DB
- Usage: first-token and total latency reported separately, adaptive ms/s display, CSV export
- Sign-in and growth centre: scheduling, manual runs, per-batch detail
- Credits monitoring, audit log, proxy keys, runtime settings, service status

**Automation**
- Daily sign-in with catch-up window and jitter
- Growth-centre tasks, lottery, travel, redemption, and the ACP conversation that lights the active day
- Proactive credential rotation (short-lived tokens refreshed before expiry)
- Usage rollup and detail retention policy

## Architecture

```
client ──▶ :9999 ──┬── /v1/*        proxy (OpenAI / Anthropic)
                   ├── /api/admin/* admin API
                   └── /admin       admin console (static assets)
                        │
                        ├── SQLite (accounts, encrypted credentials, telemetry, scheduler state)
                        └── upstream: copilot.tencent.com / www.workbuddy.ai
```

One process. The Python build ran a Control Plane and a supervised Proxy Worker
that exchanged a versioned JSON snapshot over a loopback handshake; folding both
surfaces into one binary removes the handshake, the snapshot serialization and a
second interpreter.

The security boundary is enforced by types rather than process isolation: the
proxy handlers receive only a `*ProxyPlane` (provider pools and model routing)
and cannot reach the database, the admin key or the credential master key.
Credentials are decrypted once per pool rebuild.

See [docs/design/architecture.md](docs/design/architecture.md).

## Quick start

```sh
# Three keys are required. Generate the credential key with:
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

```sh
cat > .env <<'EOF'
QB2API_PROXY_API_KEY=<proxy key used by clients>
QB2API_ADMIN_KEY=<admin console key>
QB2API_CREDENTIAL_KEY=<the Fernet key from above>
QB2API_DATA_DIR=./data
QB2API_LOG_DIR=./logs
QB2API_MODEL_CONFIG=./config/models.json
QB2API_ADMIN_UI_ENABLED=true
QB2API_ADMIN_COOKIE_SECURE=auto
EOF

go run ./cmd/qb2api
```

Open <http://127.0.0.1:9999/admin>, sign in with `QB2API_ADMIN_KEY`, and import
credentials on the Accounts page.

## Docker deployment

```sh
docker compose -f go-deploy/docker-compose.go.yml up -d --build
```

Alpine-based image holding one static binary, about 37 MB. Data, logs and the
model config are mounted as volumes.

### Switching from the Python build

```sh
cd go-deploy
./switch-to-go.sh --dry-run   # rehearses the steps, changes nothing
./switch-to-go.sh             # interactive confirmation, then switches
./switch-to-go.sh --yes       # unattended
```

The script stops the Python service, moves the Go service to port 9999, restarts
and verifies it (`/health`, `/admin`, `/v1/models`, account count, and a real
`deepseek-v4.1-flash` streaming call), and rolls back automatically if any check
fails. See [go-deploy/README.md](go-deploy/README.md).

**Existing data carries over as-is.** The Go Fernet implementation is
byte-compatible with Python's `cryptography`, so pointing the binary at the
existing `qb2api.sqlite3` is enough — **no re-login required**.

## Client usage

```sh
# OpenAI-compatible
curl -N http://127.0.0.1:9999/v1/chat/completions \
  -H "Authorization: Bearer $QB2API_PROXY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4.1-flash","stream":true,
       "messages":[{"role":"system","content":"You are a helpful assistant."},
                   {"role":"user","content":"hello"}]}'
```

```sh
# Anthropic-compatible
curl http://127.0.0.1:9999/v1/messages \
  -H "Authorization: Bearer $QB2API_PROXY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4.1-flash","max_tokens":256,
       "messages":[{"role":"user","content":"hello"}]}'
```

List models with `curl http://127.0.0.1:9999/v1/models`.

## Documentation

- [Configuration](docs/configuration.md) — every environment variable, remote access setup
- [Architecture](docs/design/architecture.md) — components, security model, data model, routing
- [Deployment and switch](go-deploy/README.md) — compose, switch script, rollback
- [Implementation notes](docs/implementation-notes.md) — storage compatibility, timestamps, write batching, percentile formulas
- [Development](CLAUDE.md) — common commands and the invariants that matter

## Development

```sh
export GOPROXY=https://goproxy.cn,direct
go vet ./... && go test ./...
cd frontend && npm install --registry=https://registry.npmmirror.com && npm test && npm run build
```

The frontend builds to `web/dist` at the repository root and is served
same-origin by the Go service.

## License

[MIT](LICENSE)
