# Spike results (external contracts)

> Generated during multi-account proxy/check-in integration.
> Status date: 2026-07-22.
> No real account credentials were exercised in CI/local automated runs.

## Matrix

| Fact ID | Topic | Status | Notes |
| --- | --- | --- | --- |
| CB-CHECKIN-01 | WorkBuddy check-in URL/method/auth, body shape, `400/code=10001` | **UNVERIFIED** | Client implements configurable paths; empty `CODEBUDDY_CHECKIN_STATUS_METHOD` skips preflight. `10001 → ALREADY_CHECKED_IN` encoded from design. Needs live cURL against authorized account. |
| QD-CHECKIN-01 | Qoder `pt_` claim vs session claim, refresh, refresh rotation | **UNVERIFIED** | Default dual credential (access/refresh) for check-in; PAT remains chat-only. Refresh accepts `device_token`/`token`. Needs live probe before enabling auto-schedule assumptions. |
| AUTH-01 | CodeBuddy OAuth `expiresIn`, `refreshToken`, refresh endpoint | **UNVERIFIED** | Start/poll paths match `workbuddy_api` reference (`/v2/plugin/auth/state|token`, pending `11217`). Refresh endpoint/rotation not implemented as verified. |

## Reference commits (local)

| Project | Path | Commit (if available) |
| --- | --- | --- |
| workbuddy_api | `/Users/dmego/vibeCoding/workbuddy_api` | see design §2.2 (`d5de25a` at design time) |
| qoderwork-account-switcher | `/Users/dmego/vibeCoding/qoderwork-account-switcher` | see design §2.2 (`022c1d4` / v1.1.0 at design time) |

## Policy

- Production code comments/tests may cite these IDs.
- Do not mark a fact **VERIFIED** until a redacted live spike records HTTP status, business code, requestId, and auth mode **without** headers/token bodies.
- Until verified: check-in purpose may store `verification_status=unverified`; scheduler should only run verified/active check-in slots.
