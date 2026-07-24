# 2api Unified Account Control Plane Refactor Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `executing-plans` and execute
> this plan task-by-task. Steps use checkbox syntax for progress tracking.

**Goal:** Preserve the existing OpenAI/Anthropic and upstream protocol adapters,
while replacing the multi-account, credential, admin, and check-in implementation
with the architecture and contracts in
`docs/design/macmini-multi-account-proxy-checkin.md`.

**Architecture:** A small FastAPI composition root owns one `RuntimeServices`
container. Durable account state is written through a transactional async SQLite
repository, secrets are encrypted by `CredentialVault`, and purpose-scoped
credentials are resolved through a versioned cache. Stable `DynamicProviderPool`
objects own 0..N account slots. Admin and proxy authentication are independent.
One serial check-in coordinator applies provider gates, bounded retry, daily
idempotency, and purpose-only state transitions.

**Tech Stack:** Python 3.11+, FastAPI, httpx, aiosqlite, cryptography Fernet,
pytest/pytest-asyncio, Ruff, native HTML/CSS/ES modules.

## Global Constraints

- The design document is the source of truth; Grok's uncommitted implementation
  is evidence, not an API contract.
- Keep `/v1/chat/completions`, `/v1/messages`, model discovery, `/api/config`,
  and the existing CodeBuddy/Qoder provider behavior backward compatible.
- `QB2API_API_KEY` is only a deprecated alias of the proxy key. It never grants
  admin access.
- Never return or log Authorization, Cookie, refresh token, PAT, Fernet key,
  session ID, or unmasked credential material.
- All durable multi-row mutations use one explicit transaction. Repository
  helpers must not commit inside an existing unit of work.
- Dynamic pool identity is stable for process lifetime. Slot health uses stable
  `(provider, account_id)` keys and a generation.
- Stream failover is allowed only before the first non-empty downstream chunk.
  After commit, propagate termination without replay or synthetic `[DONE]`.
- `CB-CHECKIN-01`, `QD-CHECKIN-01`, and `AUTH-01` remain unverified until a
  redacted live spike exists. Unverified capabilities are never scheduled.
- Python functions are at most 50 lines, Python/JS files at most 300 lines,
  nesting at most 3, and public functions have no more than 3 positional args.
- No automatic commit, push, destructive Git reset, or deletion of user work.

---

### Task 1: Freeze Compatibility and Add Contract Tests

**Files:**
- Create: `tests/integration/test_auth_matrix.py`
- Create: `tests/integration/test_stream_contract.py`
- Create: `tests/integration/test_runtime_boot.py`
- Modify: `tests/test_app.py`

**Interfaces:**
- Consumes: existing FastAPI `app`, `Settings`, `ProviderRegistry`.
- Produces: executable contract for public, proxy, legacy-admin, and protected
  admin routes; executable stream commit boundary; env-only startup contract.

- [ ] Add a fixture that builds an app with isolated settings, SQLite, and fake
  providers without reading the developer's `.env`.
- [ ] Add auth matrix tests proving proxy key cannot access `/api/admin/*` or
  `/api/config`, admin key cannot access `/v1/*`, cookie cannot access proxy or
  legacy config routes, and equal keys reject startup.
- [ ] Add tests proving empty dynamic pools return `503 provider_unavailable`,
  pre-commit stream failure uses a second slot, and post-commit failure neither
  calls another slot nor emits `[DONE]`.
- [ ] Add startup tests for env-only CodeBuddy, env-only Qoder, admin mode with
  missing keys, and disabled admin UI.
- [ ] Run `rtk pytest -q tests/integration tests/test_app.py`; confirm new tests
  fail only on the documented implementation gaps.

### Task 2: Transactional Repository and Credential Semantics

**Files:**
- Create: `src/qb2api/accounts/schema.py`
- Create: `src/qb2api/accounts/repo_accounts.py`
- Create: `src/qb2api/accounts/repo_checkin.py`
- Rewrite: `src/qb2api/accounts/repository.py`
- Modify: `src/qb2api/accounts/resolver.py`
- Modify: `src/qb2api/accounts/vault.py`
- Create: `tests/accounts/test_repository_transactions.py`
- Create: `tests/accounts/test_credential_cas.py`
- Modify: `tests/test_registry_resolver.py`

**Interfaces:**
- Produces: `AccountRepository.transaction()` async context manager;
  `upsert_credential(..., expected_version: int | None) -> int` with atomic CAS;
  account/purpose/check-in read and write methods preserving current callers.
- Produces: `CredentialResolver.credential(provider, account_id, purpose, *,
  force_refresh=False)` with version-aware cache and per-key single flight.

- [x] Add a two-connection concurrency test where exactly one writer can update
  credential version `N` to `N+1`; the loser must raise
  `CredentialVersionConflict` and must not report success.
- [ ] Add rollback tests for OAuth import and Qoder check-in import that inject a
  failure between purpose and credential writes and observe no partial account.
- [x] Move schema text out of the repository and apply WAL, foreign keys,
  `busy_timeout=5000`, and `synchronous=NORMAL` to every connection.
- [x] Implement CAS as a single `UPDATE ... WHERE credential_version=?`, check
  that statement's `rowcount`, and commit only after the result is known.
- [x] Implement explicit transaction ownership so composite services can update
  account, purposes, and credentials atomically.
- [ ] Invalidate cache only after a successful commit; preserve old cached and
  durable values after conflicts or refresh failures.
- [ ] Run `rtk pytest -q tests/accounts tests/test_accounts_vault.py
  tests/test_registry_resolver.py`.

### Task 3: Stable Dynamic Provider Pools and Runtime Assembly

**Files:**
- Rewrite: `src/qb2api/providers/lb.py`
- Create: `src/qb2api/runtime.py`
- Create: `src/qb2api/provider_factory.py`
- Modify: `src/qb2api/providers/codebuddy.py`
- Modify: `src/qb2api/providers/qoder.py`
- Modify: `src/qb2api/accounts/registry.py`
- Modify: `tests/test_dynamic_pool.py`
- Create: `tests/integration/test_pool_refresh.py`

**Interfaces:**
- Produces: `DynamicProviderPool.update_slots(slots)`, `complete(request)`,
  `stream(request)`, `close()`; `SlotKey(provider, account_id)` and request lease.
- Produces: `RuntimeServices.start(settings)`, `refresh_accounts()`, and `close()`.

- [ ] Add tests for 0/1/N slots, stable object identity, stable-key cooldown,
  generation isolation, client cancellation, replacement while in flight, and
  shutdown that does not close providers with active leases.
- [x] Ensure post-commit failures mark only the leased generation unhealthy and
  propagate. Remove synthetic successful stream termination from wrappers.
- [x] Build CodeBuddy providers with a per-attempt credential getter. Never cache
  a dynamic bearer in the pool slot.
- [x] Keep Qoder PAT and session isolated per account. Rebuild only the affected
  slot when PAT/session configuration changes.
- [x] Move global assembly out of `app.py` into `RuntimeServices`; one start and
  one close path must own repository, pools, scheduler, OAuth client, and sessions.
- [ ] Run `rtk pytest -q tests/test_dynamic_pool.py
  tests/integration/test_pool_refresh.py tests/integration/test_stream_contract.py`.

### Task 4: Admin Authentication and Session Boundary

**Files:**
- Rewrite: `src/qb2api/admin/auth.py`
- Create: `src/qb2api/admin/sessions.py`
- Create: `src/qb2api/admin/dependencies.py`
- Create: `src/qb2api/admin/session_routes.py`
- Rewrite: `src/qb2api/admin/router.py` as an include-only aggregator.
- Modify: `src/qb2api/config.py`
- Modify: `tests/test_admin_auth.py`
- Modify: `tests/integration/test_auth_matrix.py`

**Interfaces:**
- Produces: strict method/path policy, `require_admin`, `require_cookie_csrf`,
  and trusted request context shared by login and cookie policy.
- Produces: hashed server-side session storage, absolute and idle TTL, five-session
  limit, rotate/revoke/revoke-all, and one-time CSRF rotation.

- [ ] Add direct-remote HTTP tests proving spoofed `X-Forwarded-Proto` is ignored
  unless the socket peer belongs to `QB2API_TRUSTED_PROXY_NETWORKS`.
- [ ] Add tests for re-auth revoking the prior cookie, logout/logout-all, session
  limit, idle and absolute expiry, CSRF on all cookie mutations, and bearer bypass
  of CSRF only for the real admin key.
- [ ] Make `/api/config` require Admin Key unconditionally. A proxy key must return
  403 and no configured key must return 401; cookie sessions are rejected.
- [ ] Derive client IP and effective HTTPS from one trusted-proxy decision. Reject
  remote HTTP in `auto` and `false` modes.
- [ ] Keep session IDs and CSRF hashes server-side; browser storage may hold only
  the current in-memory CSRF token.
- [ ] Run `rtk pytest -q tests/test_admin_auth.py
  tests/integration/test_auth_matrix.py`.

### Task 5: Atomic Account Import, Promotion, and OAuth Lifecycle

**Files:**
- Rewrite: `src/qb2api/accounts/promote.py`
- Create: `src/qb2api/admin/account_routes.py`
- Create: `src/qb2api/admin/import_routes.py`
- Modify: `src/qb2api/auth/codebuddy_oauth.py`
- Modify: `src/qb2api/auth/flows.py`
- Create: `tests/integration/test_account_admin.py`
- Create: `tests/integration/test_import_atomicity.py`

**Interfaces:**
- Produces: list/get/patch/delete/promote/refresh/probe account routes.
- Produces: CodeBuddy OAuth start/poll and manual import; Qoder chat and check-in
  import; every response returns only `AccountView` and redacted status.

- [ ] Validate provider, label, body size, account ID ownership, purpose patch
  whitelist, and imported credential schema before opening a transaction.
- [ ] Promotion generates a random durable ID internally, atomically writes chat
  state/credential, creates check-in `needs_import` or `unconfigured`, and shadows
  the env slot only after commit.
- [ ] OAuth poll consumes a flow only after the durable transaction commits.
  Concurrent polls for the same flow may create at most one account.
- [ ] Wire the resolver refresh callback only after `AUTH-01` is verified. Until
  then, expired OAuth access transitions chat/check-in independently to
  `needs_reauth`; never pretend refresh support exists.
- [ ] Qoder check-in import accepts only a recognized successful status response.
  Transport, 429, 5xx, malformed 2xx, unknown state, or identity mismatch must not
  overwrite the existing credential or mark verification as successful.
- [ ] Run `rtk pytest -q tests/integration/test_account_admin.py
  tests/integration/test_import_atomicity.py tests/test_codebuddy_oauth.py`.

### Task 6: Purpose-Isolated Check-in Engine and Scheduler

**Files:**
- Rewrite: `src/qb2api/checkin/base.py`
- Split: `src/qb2api/checkin/qoder.py` into client and parser helpers.
- Rewrite: `src/qb2api/checkin/service.py`
- Create: `src/qb2api/checkin/retry.py`
- Modify: `src/qb2api/checkin/scheduler.py`
- Create: `src/qb2api/admin/checkin_routes.py`
- Modify: `tests/test_checkin_clients.py`
- Create: `tests/checkin/test_retry_policy.py`
- Create: `tests/integration/test_checkin_service.py`
- Create: `tests/integration/test_scheduler.py`

**Interfaces:**
- Produces: `CheckinService.run_batch(trigger, targets=None,
  skip_already_done=True)` with one process lock and per-account isolation.
- Produces: retry policy that retries only transport, timeout, 429, and
  502/503/504 up to `CHECKIN_RETRY_LIMIT`, with capped `Retry-After` and jitter.

- [ ] Add tests for WorkBuddy `400/code=10001`, no status preflight when method is
  empty, per-account Cookie isolation, and no retry of terminal/business 4xx.
- [ ] Add Qoder tests for recognized claimable/already states, refresh rotation,
  CAS conflict reload, refresh failure, malformed status, and no chat mutation.
- [ ] Filter scheduled accounts by both global and provider-specific enable flags,
  `purpose.status=active`, and `verification_status=verified`. Explicit admin
  verification may target an unverified account but ordinary manual batches may not.
- [ ] Persist every attempt and terminal daily state. Cancellation or shutdown
  must finish the run as `cancelled`, not leave a permanent `running` row.
- [ ] Implement one scheduler task, bounded catch-up, one shared batch lock, and
  deterministic next-run reporting.
- [ ] Run `rtk pytest -q tests/test_checkin_clients.py tests/checkin
  tests/integration/test_checkin_service.py tests/integration/test_scheduler.py`.

### Task 7: Thin FastAPI Composition and Complete Admin UI

**Files:**
- Rewrite: `src/qb2api/app.py` as composition root under 300 lines.
- Create: `src/qb2api/api/public.py`
- Create: `src/qb2api/api/proxy.py`
- Create: `src/qb2api/api/legacy_config.py`
- Split: `src/qb2api/web/admin.js` into ES modules under 300 lines each.
- Modify: `src/qb2api/web/admin.html`
- Modify: `src/qb2api/web/admin.css`
- Create: `tests/integration/test_admin_ui.py`

**Interfaces:**
- Produces: `create_app(settings_factory=Settings.from_env) -> FastAPI` for tests
  and one module-level `app` for Uvicorn compatibility.
- Produces: UI workflows for login, account list/actions, OAuth, import,
  verification, targeted/full check-in, run details, and read-only settings.

- [ ] Move proxy and legacy-config handlers out of `app.py`; handlers obtain
  `RuntimeServices` from `app.state` and do not import mutable module globals.
- [ ] Keep `/admin` shell public but return no account/config data. Honor
  `QB2API_ADMIN_UI_ENABLED`; disabled deployments return 404 for the shell/assets.
- [ ] Render all upstream/account strings with `textContent`, never untrusted
  `innerHTML`. Keep CSRF only in module memory and clear it on logout/page unload.
- [ ] Provide visible loading/error/empty states, keyboard labels, focus handling,
  responsive tables, and confirmation for destructive account deletion.
- [ ] Add an HTML smoke test for static resources and API integration tests proving
  anonymous UI access cannot load protected state.
- [ ] Run `rtk pytest -q tests/integration/test_admin_ui.py tests/test_app.py
  tests/test_anthropic.py`.

### Task 8: Migration, Operations, and Final Acceptance

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `README.zh.md`
- Rewrite: `tools/qoder-checkin-exporter/README.md`
- Replace: `tools/qoder-checkin-exporter/export_stub.py` with an explicitly named
  schema validator unless a separately audited Windows exporter is implemented.
- Update: `docs/spike/spike-results.md`
- Update: `.superpowers/sdd/progress.md`

**Interfaces:**
- Produces: documented migration from env tokens, backup/restore instructions,
  HTTPS/Tailscale deployment contract, exporter/import runbook, and fact matrix.

- [ ] Document every new environment variable and mark legacy alias behavior.
- [ ] Ensure the exporter tool cannot be mistaken for a functional Windows token
  extractor while `QD-CHECKIN-01` remains unverified.
- [ ] Run `rtk pytest -q` and record exact pass/fail count.
- [ ] Run `rtk ruff check .` and require zero issues.
- [ ] Run `rtk python -m compileall -q src tools` and require exit code 0.
- [ ] Build wheel/sdist with `rtk python -m build` when the `build` package is
  available; otherwise record the missing prerequisite without claiming success.
- [ ] Start the app once in isolated env-only mode and once in admin mode with a
  temporary SQLite directory; probe `/health`, `/v1/models`, `/admin`, session
  bootstrap, and clean shutdown without real upstream credentials.
- [ ] Re-read design section 20 and write a checked acceptance report. Keep live
  Spike-dependent items unchecked until authorized credentials are exercised.
- [ ] Run `rtk git diff --check` and `rtk git status --short --untracked-files=all`.

## Completion Gate

The refactor is not complete merely because local unit tests pass. Completion
requires all deterministic tests, Ruff, compile/build checks, two startup smokes,
and a line-by-line design section 20 report. Live WorkBuddy/Qoder/OAuth protocol
claims remain explicitly unverified until redacted real-account evidence exists.
