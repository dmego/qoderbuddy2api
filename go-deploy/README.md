# Go rewrite deployment

## Production switch (current state)

The Go build is the production service on **9999**. The Python container is
stopped but retained, and its data directory is untouched.

| | Python (stopped, retained) | Go (production) |
|---|---|---|
| container | `qb2api-control` | `qb2api-go` |
| host port | — (was 9999) | **9999** |
| data | `data/qb2api.sqlite3` (untouched) | `go-data/qb2api.sqlite3` |
| image | `qb2api-control:local` | `qb2api-go:local` |

Switch, verify and roll back with:

```sh
cd ~/docker-space/qoderbuddy2api-go
./switch-to-go.sh --dry-run   # print the planned actions, change nothing
./switch-to-go.sh             # interactive confirmation, then switch
./switch-to-go.sh --yes       # unattended
```

The script stops Python, moves the Go service to 9999, restarts it, verifies
(`/health`, `/admin`, `/v1/models`, account count, and a real
`deepseek-v4.1-flash` streaming call), and rolls back automatically if any check
fails. It is idempotent, backs the two config files up to
`switch-backup-<timestamp>/`, and logs to `switch-<timestamp>.log`.

Rollback by hand:

```sh
docker compose -f ~/docker-space/qoderbuddy2api/docker-compose.yml start
docker compose -f ~/docker-space/qoderbuddy2api-go/docker-compose.go.yml down
# then restore the two files from switch-backup-<timestamp>/
```

## Running both side by side (pre-switch layout)

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

Port note: 9998 is bound on this host by an unrelated service
(`homework/rca-resarch-net/tools/net_sync.py`, listening on `0.0.0.0:9998`),
which shadows a container published on the same port because a specific-address
bind wins over a wildcard one. The side-by-side layout therefore used 9997; the
production switch moved the Go service to 9999 once Python stopped.

`Dockerfile.go` lives at the repository root and takes the repository root as
its build context, so compose resolves it relative to this file's directory.

## 4. Verify

```sh
# health (public)
curl --noproxy '*' http://127.0.0.1:9999/health

# unified model list (needs the proxy key)
curl --noproxy '*' -H "Authorization: Bearer $QB2API_PROXY_API_KEY" \
  http://127.0.0.1:9999/v1/models

# signed-in accounts, read back out of the copied database
curl --noproxy '*' -H "Authorization: Bearer $QB2API_ADMIN_KEY" \
  http://127.0.0.1:9999/api/admin/accounts

# the console
open http://127.0.0.1:9999/admin
```

A working end-to-end call:

```sh
curl --noproxy '*' -N -H "Authorization: Bearer $QB2API_PROXY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4.1-flash","stream":true,"messages":[{"role":"system","content":"You are a helpful assistant."},{"role":"user","content":"say hi"}]}' \
  http://127.0.0.1:9999/v1/chat/completions
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
