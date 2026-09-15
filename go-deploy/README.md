# Go rewrite deployment — runs alongside the Python stack

Both stacks run at the same time, on different ports, against separate data
copies. Nothing here touches the running Python deployment.

| | Python (unchanged) | Go rewrite |
|---|---|---|
| container | `qb2api-control` | `qb2api-go` |
| host port | `9999` | `9997` |
| data | `data/qb2api.sqlite3` | `go-data/qb2api.sqlite3` |
| image | `qb2api-control:local` | `qb2api-go:local` |

## 1. Seed the Go data copy from the live database

The existing credentials are encrypted with the Fernet key in `QB2API_CREDENTIAL_KEY`;
the Go vault reads that format, so copying the database is enough — **no re-login**.

```sh
cd ~/docker-space/qoderbuddy2api
mkdir -p go-data go-logs
sqlite3 data/qb2api.sqlite3 ".backup 'go-data/qb2api.sqlite3'"
```

`.backup` takes a consistent snapshot of a live WAL database, so the Python
service does not need to stop. Re-run it whenever you want to refresh the copy.

## 2. Provide the Go environment file

```sh
sed \
  -e 's|^QB2API_CONTROL_PORT=.*|QB2API_CONTROL_PORT=9997|' \
  -e 's|^QB2API_PORT=.*|QB2API_PORT=9997|' \
  .env > .env.go
chmod 600 .env.go
```

Everything else — the admin key, the proxy key, the credential key, the model
config, the check-in schedule — is read unchanged. The credential key MUST stay
identical or the copied credentials cannot be decrypted.

Optional Go-only knobs:

| variable | default | purpose |
|---|---|---|
| `QB2API_EVENT_FLUSH_MILLIS` | `250` | telemetry batching window; a larger value means fewer, bigger writes |
| `QB2API_EVENT_FLUSH_MAX` | `200` | events per batch before an immediate flush |

## 3. Build and start

```sh
cd ~/docker-space/qoderbuddy2api
docker compose -f docker-compose.go.yml up -d --build
```

Port note: 9998 was the first choice but is already bound on this host by an
unrelated service (`homework/rca-resarch-net/tools/net_sync.py`, listening on
`0.0.0.0:9998`), which shadowed the container because a specific-address bind
wins over a wildcard one for the same port. The deployment therefore uses 9997,
which was verified free.

`Dockerfile.go` lives at the repository root and takes the repository root as
its build context, so compose resolves it relative to this file's directory.

## 4. Verify

```sh
# health (public)
curl --noproxy '*' http://127.0.0.1:9997/health

# unified model list (needs the proxy key)
curl --noproxy '*' -H "Authorization: Bearer $QB2API_PROXY_API_KEY" \
  http://127.0.0.1:9997/v1/models

# signed-in accounts, read back out of the copied database
curl --noproxy '*' -H "Authorization: Bearer $QB2API_ADMIN_KEY" \
  http://127.0.0.1:9997/api/admin/accounts

# the console
open http://127.0.0.1:9997/admin
```

A working end-to-end call:

```sh
curl --noproxy '*' -N -H "Authorization: Bearer $QB2API_PROXY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4.1-flash","stream":true,"messages":[{"role":"system","content":"You are a helpful assistant."},{"role":"user","content":"say hi"}]}' \
  http://127.0.0.1:9997/v1/chat/completions
```

The request path must never be pointed at the local TUN proxy. Both the Go
upstream clients and the Python ones set `Proxy: nil` / `trust_env=False` for
exactly that reason; if you add a client, do the same.

## 5. Stop / remove

```sh
docker compose -f docker-compose.go.yml down          # stop, keep data
docker compose -f docker-compose.go.yml down -v       # also drop the volume
```

The Python stack on 9999 is unaffected by either command.
