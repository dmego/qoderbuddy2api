# qoderbuddy2api

`qoderbuddy2api` is a local operations platform for CodeBuddy and Qoder CN
accounts. A persistent Control Plane owns the administration UI, encrypted
credentials, SQLite, scheduling, backup, and Worker supervision. Its Proxy
Worker serves OpenAI- and Anthropic-compatible model traffic.

Python 3.11+ is required. The product is for one trusted operator on a Mac
Mini or development machine, not for public multi-tenant Internet exposure.

## Topology and credentials

```text
Browser (admin) ─────> Control Plane :9999
                         ├─ Admin UI, SQLite, audit, backup, supervisor
                         └─ Proxy Worker 127.0.0.1:10001 ──> /v1, /v1/messages
```

The Control Plane is the only persistent service. It starts and owns the
Worker; do not create a second launchd/systemd Worker unit. The Worker normally
remains loopback-only and does not open SQLite or receive the Admin Key or the
credential encryption key.

Use three different values:

| Variable | Scope |
| --- | --- |
| `QB2API_PROXY_API_KEY` | Model client requests to the Worker |
| `QB2API_ADMIN_KEY` | Initial admin login and admin automation |
| `QB2API_CREDENTIAL_KEY` | Encryption of durable provider credentials |

Never put raw keys, tokens, cookies, Authorization values, prompts, or
completions in a URL, browser LocalStorage/sessionStorage, Git, screenshots,
or ordinary logs.

## Local quick start

```bash
git clone https://github.com/dmego/qoderbuddy2api.git
cd qoderbuddy2api
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
chmod 600 .env
mkdir -p data logs && chmod 700 data logs
```

Generate two independent HTTP keys and a Fernet credential key locally, then
put them in `.env`:

```bash
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'
.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Set `QB2API_ADMIN_KEY` and `QB2API_CREDENTIAL_KEY` before enabling the Admin
UI, durable accounts, check-in, or backup. Start from the repository root so
the process reads `.env`:

```bash
.venv/bin/qb2api --mode control
```

Default addresses:

- Control health: `http://127.0.0.1:9999/health`
- Admin UI: `http://127.0.0.1:9999/admin/`
- Worker models: `http://127.0.0.1:10001/v1/models`
- OpenAI base URL: `http://127.0.0.1:10001/v1`
- Anthropic Messages: `http://127.0.0.1:10001/v1/messages`

Model clients use the Worker address and send the Proxy Key only through an
`Authorization: Bearer …` header. The Control Plane deliberately does not
proxy `/v1` traffic.

## Trusted remote HTTP and HTTPS

HTTPS is recommended for every non-loopback browser. With the default
`QB2API_ADMIN_COOKIE_SECURE=auto`, loopback HTTP is allowed and remote HTTP
login is rejected.

Trusted Tailscale/LAN HTTP is explicitly supported when TLS is unavailable:

```ini
QB2API_CONTROL_HOST=100.101.102.103
QB2API_CONTROL_PORT=9999
QB2API_ADMIN_COOKIE_SECURE=false
```

This is a deliberate transport-risk exception: the session remains `HttpOnly`
and `SameSite=Lax`, but the first Admin Key login is not encrypted. Use it only
on a trusted tailnet/LAN. Never combine it with public DNS, public port
forwarding, shared Wi-Fi, or a public reverse proxy.

For HTTPS, bind Control Plane to loopback and use a TLS proxy. Only after its
direct peer CIDR is known should it trust forwarded headers:

```ini
QB2API_CONTROL_HOST=127.0.0.1
QB2API_ADMIN_COOKIE_SECURE=auto
QB2API_TRUSTED_PROXY_HEADERS=true
QB2API_TRUSTED_PROXY_NETWORKS=127.0.0.1/32
```

The reverse proxy must overwrite `X-Forwarded-For` and
`X-Forwarded-Proto`; never enable this trust for arbitrary clients or a broad
network range.

## Operations, backup, and smoke

Use the Service page (or the Admin-Key-protected service API) only to manage
the Worker. Restart the Control Plane through launchd/systemd. A Control Plane
restart stops its Worker and deliberately revokes browser sessions, so signing
in again is expected.

Backup restore is validation-only through the API: it checks checksum, SQLite
integrity, and schema compatibility, then returns `offline_restore_required`.
Actual restoration requires the Control Plane to be stopped before copying a
validated SQLite backup over the active database.

```bash
PYTHON_BIN=.venv/bin/python bash scripts/smoke_fresh_install.sh
PYTHON_BIN=.venv/bin/python bash scripts/smoke_migrated_install.sh
```

The smokes use a temporary data directory, verify Control/Worker startup, a
Worker crash followed by supervised restart, and backup dry-run validation.
They delete their artifacts unless `QB2API_SMOKE_KEEP=1` is set.

## Runbooks

- [Mac Mini deployment and operations](docs/deployment/macmini.md)
- [Single-process migration](docs/migration/single-process-to-control-worker.md)
- [launchd Control Plane template](deploy/launchd/cn.qb2api.control.plist)
- [optional systemd development template](deploy/systemd/qb2api-control.service)
- [Qoder Windows check-in exporter](tools/qoder-checkin-exporter/README.md)

Static environment tokens are transient chat slots. Use the Admin UI to
promote/import an account that needs durable identity, check-in, or credential
rotation. Qoder chat PATs and Qoder check-in access/refresh credentials are
different values. Import WorkBuddy check-in credentials from
`/admin/accounts/add` with the CodeBuddy / WorkBuddy Check-in workflow. It
accepts bearer, cookie, or bearer + cookie mode and persists only after the
server validates success or an already-checked-in result. Never store those
credentials in `.env`, a URL, browser storage, or a generic endpoint.

The administrative console is a high-density local control plane. Its grouped
navigation covers runtime, account pool, proxy/models, automation, and
governance. On a phone the navigation is an explicit drawer; table data remains
scrollable and dangerous operations retain their confirmation dialogs. The UI
does not persist an Admin Key or provider credentials in browser storage.

## Development verification

```bash
pytest -q
ruff check src tests
python -m compileall -q src/qb2api
cd frontend && npm run test && npm run typecheck && npm run lint && npm run build && npm run test:e2e
git diff --check
```

## License

MIT — see [LICENSE](LICENSE).
