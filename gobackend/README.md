# qoderbuddy2api — Go backend (`gobackend/`)

A from-scratch Go implementation of the control plane and proxy. It runs
alongside the Python deployment, on its own port, against a copy of the same
database. See `go-deploy/README.md` for the deployment procedure.

## Why the rewrite

The Python build ran two processes (Control Plane + supervised Proxy Worker)
that exchanged a versioned JSON snapshot over a loopback handshake, each paying a
full interpreter and dependency set. The Go build serves both surfaces from one
static binary. Concretely:

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

## Scope

Kept providers, both WorkBuddy deployments:

| provider id | product | upstream |
|---|---|---|
| `codebuddy` | WorkBuddy (domestic) | `copilot.tencent.com` for chat, `www.workbuddy.cn` for check-in/growth |
| `workbuddy_intl` | WorkBuddy International | `www.workbuddy.ai` |

Dropped: `qoder` and `orcaterm`, along with their model-sync schedulers, device
token derivation, and the `qoder_checkin_disabled` / `qoder_checkin_reauth_required`
business codes. The frontend no longer offers them.

The provider id `codebuddy` is deliberately **not** renamed to `workbuddy`:
accounts, credentials, purposes, routing policies and every historical
telemetry row are keyed by it, and renaming it would orphan all of that.

## Layout

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

## Storage compatibility

`internal/store/schema.go` reproduces the Python DDL exactly — same tables, same
columns, same types, same constraint names — so the binary points at the existing
`qb2api.sqlite3` with no migration step. New columns are additive and applied by
`Migrate`, which is idempotent against a populated database.

Two write-volume changes address the hot spots measured on the Python build:

1. **Request telemetry is batched.** The Python worker queued events and the
   control plane issued one transaction (and WAL fsync) per request. The Go
   event writer coalesces up to `QB2API_EVENT_FLUSH_MAX` events (default 200) or
   `QB2API_EVENT_FLUSH_MILLIS` (default 250 ms) into a single transaction.
   Telemetry is never allowed to block or fail a proxied request; a failed batch
   is counted as dropped, not retried into a growing backlog.
2. **Metric history no longer stores per-package detail.** `packages` was ~97% of
   the `points` payload bytes while the history trend only reads scalar totals.
   `account_metric_snapshots` keeps the full payload (the credits detail page
   renders it); `account_metric_history` stores the payload with `packages`
   removed. `main.compactHistory` rewrites rows written by the Python collector
   on first start.

`internal/store/telemetry.go` also ports the **two different percentile
formulas** the Python build used — `/usage/summary` uses `ceil(n*f)-1`, the
rollup aggregator uses `round((n-1)*f)` with banker's rounding — because they
genuinely disagree for some sample counts and unifying them would silently
change numbers the console already displayed.

## Timestamps

Every stored timestamp uses `2006-01-02T15:04:05+00:00` (`store.ISOFormat`), which
is what Python's `datetime.now(UTC).replace(microsecond=0).isoformat()` emits.
This is **not** RFC3339: there is no `Z` form. Range filters compare these values
lexicographically, so a `Z`-suffixed value would break them.

## Latency telemetry

`request_events.first_token_ms` (new column) records time-to-first-token: the
elapsed time when the first content-bearing SSE frame reaches the client, which
is distinct from `latency_ms` (total request duration). Both feed the usage page,
which renders them through an adaptive formatter (`<1 s` as ms, `>=1 s` as
seconds, `>=60 s` as `m s`) so a long reasoning request does not print six-digit
milliseconds.

## Environment

All variable names are unchanged from the Python build. Go-only additions:

| variable | default | purpose |
|---|---|---|
| `QB2API_EVENT_FLUSH_MILLIS` | `250` | telemetry batch window |
| `QB2API_EVENT_FLUSH_MAX` | `200` | events per batch |
| `QB2API_WEB_DIR` | auto | admin console directory override |

`QB2API_CREDENTIAL_KEY` is required: without it stored credentials cannot be
decrypted and the process refuses to start.

## Development

```sh
cd gobackend
export GOPROXY=https://goproxy.cn,direct   # proxy.golang.org is unreachable here
go build ./...
go vet ./...
go test ./...
```

## Configuration notes

- Upstream HTTP clients set `Proxy: nil` on the transport. The deployment host
  runs a TUN-mode proxy; a stale pooled connection through it cost 200+ second
  stalls in the Python build, and Go's default transport would inherit the same
  environment variables.
- One process serves the proxy and the console, so `QB2API_PORT` is the only
  listener that matters. `QB2API_CONTROL_PORT`/`QB2API_WORKER_PORT` are still
  read so an existing `.env` needs no edits.
