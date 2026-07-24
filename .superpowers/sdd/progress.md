# SDD Progress Ledger

- Goal: implement `docs/design/macmini-multi-account-proxy-checkin.md` fully
- Active plan: `docs/superpowers/plans/2026-07-24-2api-completion-plan.md`
- Branch: `codex/multi-account-proxy-checkin`
- Execution: root integrator plus up to three isolated-worktree subagents; one commit per large delivery task
- Last saved: `2026-07-24` Task 8 integrated and verified

## Approved architecture

- Persistent FastAPI Control Plane + independent loopback Proxy Worker
- `ServiceSupervisor` owns Worker lifecycle and validates PID/start-time/owner/process-group/internal-token
- Vue 3 operations console manages service, accounts, credentials, models, usage, token/points/quota, check-in, runtime settings, audit and backup
- Single-admin local console; no regular users, billing, payments, plans or redeem codes
- External protocol gates remain `CB-CHECKIN-01`, `QD-CHECKIN-01`, and `AUTH-01`

## Status

### Active 9-task completion plan

- Tasks 1 through 7 are integrated and passed their Wave 1 and Wave 2 gates.
- Task 8 code-quality and legacy cleanup is integrated. Progress: **8/9
  delivery tasks integrated**.
- Task 8 removed legacy admin assets and production source maps, split persistence,
  registry, admin, runtime, protocol, Provider and Control lifecycle duties, and
  split all remaining oversized test suites.
- The repository code-limit checker now enforces production file/function,
  complexity, nesting and positional-argument limits plus the 300-line test-file
  limit. The current repository has zero violations.
- Task 8 full gate passed: 281 Python tests, 32 frontend tests, Ruff,
  compileall, typecheck, ESLint, production build, fresh/migrated real-process
  smoke, source-map absence and `git diff --check`.
- Task 9 browser acceptance, final documentation alignment and final cleanup is
  the only remaining delivery task. The project is not yet complete.

## Existing implementation evidence

- `create_control_app` serves the Vue SPA same-origin; `create_worker_app` wraps the existing OpenAI/Anthropic compatibility surface while handler extraction remains in progress
- `RuntimeServices` owns Control-only check-in, metrics, backup and rollup services; Worker emits bounded internal telemetry
- Credentials rotate/revoke atomically with purpose state, invalidate resolver cache, rebuild dynamic pools, and return only metadata
- Check-in history is now persisted and exposed as bounded secret-free run summaries plus separate attempt detail
- Vue console uses route-level code splitting and modular ECharts; the primary shell is about 144 KB minified, chart code is lazy
- No push has been performed; all work remains on local task/integration branches.

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

1. Tasks 1 through 8 are integrated on `codex/multi-account-proxy-checkin`.
2. Wave 1, Wave 2 and Task 8 Python/frontend/static/build gates passed.
3. Execute Task 9 browser acceptance and final completion audit.

## Deliberately deferred at this checkpoint

- No push, branch rewrite, or destructive cleanup has been performed; the current code baseline is committed at `703cdfc`.
- Trusted remote HTTP is explicitly supported only with
  `QB2API_ADMIN_COOKIE_SECURE=false`; secure-by-default `auto` remains enforced.

## Commit checkpoints

- `703cdfc feat: add control plane refactor baseline`
- `545f44e docs(plan): define parallel completion work`
- `d68ea72 chore: ignore task worktrees`
- `59f1a99 feat(security): close admin and proxy access`
- `6b09701 fix(security): enforce proxy key lifecycle`
- `13dd5d8 fix(security): surface pending key revocation`
- Task 1 final review: Approved; no remaining Critical, Important or Minor.
- `f01555b refactor(checkin): harden provider protocols`
- `339db2b fix(checkin): close refresh and export races`
- Task 3 final review: Approved; no remaining Critical or Important.
- `6415805 feat(admin): complete management contracts`
- `47dd7f4 fix(admin): close management contract gaps`
- `b7848e0 fix(integration): align management contracts`
- `57c4a4e feat(console): complete operations workflows`
- `dd13ab3 fix(console): align operations contracts`
- `c701bd1 fix(console): harden operations feedback`
- Wave 1 gate at `c701bd1`: 266 Python tests and 30 frontend tests passed;
  Ruff, compileall, typecheck, ESLint, production build and diff check passed.
- `3887fd3 feat(accounts): close onboarding workflows`
- `cd79b52 feat(ops): close schedulers and recovery`
- `3b92098 docs(deploy): add migration and runbooks`
- Wave 2 integration fix: async check-in contract test alignment and busy-run
  failure audit with stable `checkin_run_in_progress` error code.
- Wave 2 gate: 278 Python tests and 32 frontend tests passed; Ruff, compileall,
  typecheck, ESLint, production build, fresh/migrated smoke, launchd plist and
  diff checks passed.
- `42d268d refactor(web): remove legacy admin assets`
- `32ffde9 refactor(runtime): reduce complexity debt`
- `27f5016 refactor(accounts): split persistence duties`
- `1964dc1 refactor(runtime): close code limit gaps`
- `5322c97 test: split oversized suites`
- Task 8 gate: 281 Python tests and 32 frontend tests passed; Ruff, compileall,
  repository code limits, typecheck, ESLint, production build, source-map
  absence, fresh/migrated smoke and diff checks passed.
