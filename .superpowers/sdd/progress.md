# SDD Progress Ledger

- Goal: implement `docs/design/macmini-multi-account-proxy-checkin.md` fully
- Active plan: `docs/superpowers/plans/2026-07-23-full-control-plane-refactor.md`
- Branch: `codex/multi-account-proxy-checkin`
- Execution: single agent on the existing branch; no auto commits or pushes
- Last saved: `2026-07-24 09:30 CST`

## Approved architecture

- Persistent FastAPI Control Plane + independent loopback Proxy Worker
- `ServiceSupervisor` owns Worker lifecycle and validates PID/start-time/owner/process-group/internal-token
- Vue 3 operations console manages service, accounts, credentials, models, usage, token/points/quota, check-in, runtime settings, audit and backup
- Single-admin local console; no regular users, billing, payments, plans or redeem codes
- External protocol gates remain `CB-CHECKIN-01`, `QD-CHECKIN-01`, and `AUTH-01`

## Status

- Expanded unique design baseline: complete and self-reviewed
- Full implementation plan: complete and self-reviewed
- Task 1 frontend build contract: complete
- Task 2 Control Plane/Worker split: complete for the current boundary; independent factories, process boundary, Worker-owned proxy handlers, versioned runtime snapshot/handshake, memory-only Worker runtime, and the 8-line compatibility entrypoint are implemented and covered by a real TCP two-process smoke
- Task 3 Supervisor lifecycle: core complete; persistent service state, operation records, loopback internal routes, authenticated reload, and identity-checked Worker startup/shutdown are implemented; additional crash/drain/timeout edge-case coverage remains
- Task 4 persistence/runtime settings: core schema migrations, WAL repository boundary, runtime settings application, backup validation and repository/transaction tests implemented; migrated-data smoke remains
- Task 5 account/pool hardening: purpose-scoped registry/resolver, stable pool, pre-first-chunk failover and credential CAS implemented; final retirement/cancellation audit remains
- Task 6 full management APIs: in progress; service/accounts/import/credentials/models/usage/metrics/settings/audit/backup/check-in history domains exist; Usage now includes compound filters, safe event detail, timeseries and audited CSV export; Proxy API Key create/list/revoke/rotate is implemented with one-time reveal and hash-only Worker verification, while pagination/no-secret tests remain incomplete across every mutation; legacy `/api/config` is read-only and Admin-Key-only during migration
- Task 7 telemetry/metrics/check-in: Worker telemetry, rollups, token/quota/points snapshots, retry/backoff and schedulers implemented; external WorkBuddy/Qoder protocol spikes remain explicit gates
- Task 8 full Vue console: in progress; authenticated Vue routes for overview, service, accounts, credentials, models, usage, check-in, settings and audit/backup exist; Usage filters, shared summary/trend/event queries, safe event detail and filtered CSV export are complete; Proxy Key management UI, visual browser acceptance and remaining detail workflows are pending
- Task 9 migration/deployment: pending
- Task 10 final review/gates: pending

## Existing implementation evidence

- `create_control_app` serves the Vue SPA same-origin; `create_worker_app` wraps the existing OpenAI/Anthropic compatibility surface while handler extraction remains in progress
- `RuntimeServices` owns Control-only check-in, metrics, backup and rollup services; Worker emits bounded internal telemetry
- Credentials rotate/revoke atomically with purpose state, invalidate resolver cache, rebuild dynamic pools, and return only metadata
- Check-in history is now persisted and exposed as bounded secret-free run summaries plus separate attempt detail
- Vue console uses route-level code splitting and modular ECharts; the primary shell is about 144 KB minified, chart code is lazy
- No commit or push has been performed

## Verified 2026-07-23

- `rtk npm run typecheck`: exit 0
- `rtk npm run test`: 4 tests passed
- `rtk npm run lint`: exit 0
- `rtk npm run build`: exit 0; Vite emitted code-split assets in `src/qb2api/web/dist`
- `rtk pytest -q tests/integration/test_admin_ui.py ... test_stream_contract.py`: 43 passed
- `rtk pytest -q tests/control/test_supervisor.py ... test_auth_matrix.py`: 9 passed
- `rtk pytest -q tests/integration/test_account_admin.py tests/accounts/test_repository_transactions.py tests/test_accounts_vault.py tests/metrics/test_metrics_scheduler.py tests/usage/test_rollups.py`: 20 passed
- Focused Ruff for control/worker/runtime/config/CLI tests: no issues
- Focused Ruff for accounts/admin/check-in/control/worker and related tests: no issues after import formatting
- `rtk python -m compileall -q src/qb2api`: exit 0
- Supervisor integration smoke started and stopped a real Worker; no listener/process remained on port 10001
- Worker handler extraction regression: 49 focused Python tests pass, including Worker OpenAI/Anthropic/SSE/model routing, import boundaries, Control/Worker role isolation, legacy config auth, and compatibility app factory
- `src/qb2api/app.py` reduced from 848 lines to 8-line Control Plane facade; no remaining tests import its legacy global proxy state
- Full Python suite after the Control/Worker facade: `rtk pytest -q` reports 209 passed
- Usage API regression: 9 focused tests pass for compound filters, safe event detail, timeseries, CSV export, audit and rollups
- Usage console regression: frontend unit tests, typecheck, lint and production build pass; the rebuilt route exposes the same filters for summary, trend, events and CSV export
- `RuntimeSnapshotService` now sends versioned models, account slots, and hashed Proxy API Keys over the authenticated loopback handshake; Worker startup does not open SQLite, load the vault, or receive raw proxy credentials
- `ServiceSupervisor` reload now performs an authenticated Worker runtime reload and checks owner/auth-version/snapshot-version; a real TCP two-process smoke covered handshake, ready, models, reload, and shutdown
- Proxy API Key route regression: `rtk pytest -q tests/integration/test_proxy_keys.py` reports 3 passed, covering secret-safe create/list/revoke and atomic rotation
- Latest core checkpoint: `rtk pytest -q tests/integration/test_proxy_keys.py tests/control/test_runtime_snapshot.py tests/integration/test_two_process_runtime.py tests/control/test_supervisor.py tests/integration/test_auth_matrix.py` reports 15 passed
- Latest frontend checkpoint: `rtk npm run test` reports 4 tests passed; `rtk git diff --check` produced no diagnostics
- `repo_control.py` is currently 294 lines and remains within the repository file-size limit, but Proxy Key persistence should be split if further additions would exceed 300 lines

## Verified 2026-07-24 baseline commit gate

- `rtk pytest -q`: 221 passed
- `rtk ruff check src tests`: no issues found after import/style-only cleanup
- `rtk python -m compileall -q src/qb2api`: exit 0
- `rtk npm run test`: 4 tests passed
- `rtk npm run typecheck`: exit 0
- `rtk npm run lint`: exit 0
- `rtk npm run build`: exit 0
- `rtk git diff --check`: no diagnostics
- Secret-pattern scan returned only variable-expression false positives in `accounts/registry.py` and `accounts/resolver.py`; no hard-coded credential was identified
- Known file-size gate failures remain and must be planned explicitly: `tests/test_app.py`, `tests/test_checkin_clients.py`, `checkin/qoder.py`, `accounts/schema.py`, `accounts/repo_accounts.py`, `providers/qoder.py`, and legacy `web/admin.js`/`web/admin.css` exceed 300 lines

## Resume checkpoint

1. Implement and test the explicit remote-HTTP admin session mode requested by the user: keep `auto` secure-by-default, allow `QB2API_ADMIN_COOKIE_SECURE=false` for trusted Tailscale/LAN HTTP, show a visible security warning, and update the design/deployment docs.
2. Finish the Proxy API Key frontend workflow and extend the real two-process smoke so a newly created key authenticates the Worker and a revoked key is rejected after snapshot reload.
3. Finish remaining management API contracts: pagination/filter validation, model probing, account detail actions, metrics detail, service events, and mutation audit/no-secret coverage.
4. Run browser desktop/mobile screenshots and console-error checks against a real Control Plane; fix overflow, empty states, and interaction gaps.
5. Add fresh-data/migrated-data smoke, deployment/HTTP access documentation, compatibility checks, and remove only confirmed unused code/assets after reference search.
6. Rerun the full Python and frontend gates, inspect all files against the 300-line limit, and record exact results before considering the refactor complete.

## Deliberately deferred at this checkpoint

- No commit, push, branch rewrite, or destructive cleanup was performed.
- Remote HTTP behavior is not yet changed; the current implementation still follows the existing secure-cookie policy.
- The design document still needs its deployment/session wording aligned with the trusted remote-HTTP option.
