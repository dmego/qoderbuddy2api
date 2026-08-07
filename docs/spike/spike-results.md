# Spike results (external contracts)

> Generated during multi-account proxy/check-in integration.
> Status date: 2026-07-24.
> No real account credentials were exercised in CI/local automated runs.

## Matrix

| Fact ID | Topic | Status | Notes |
| --- | --- | --- | --- |
| CB-CHECKIN-01 | WorkBuddy check-in URL/method/auth, body shape, `400/code=10001` | **RUNTIME UNVERIFIED** | Local `workbuddy_api` commit `d5de25a` contains OAuth/chat Bearer handling but no daily-checkin implementation. The POST and `10001` sample remain user-observed design inputs, not a local-source or runtime confirmation. Status preflight therefore stays disabled unless configured. |
| QD-CHECKIN-01 | Qoder status/claim/refresh and refresh rotation | **REFERENCE OBSERVED; RUNTIME UNVERIFIED** | Clean local switcher commit `022c1d4` defines GET status, POST claim `{}`, POST refresh, `Authorization: Bearer`, `User-Agent: QoderWork`, status/result fields, and `device_token`/`token` refresh fallback. It accepts an optional rotated refresh field in the response struct but its helper discards it. No upstream request was run here. |
| AUTH-01 | CodeBuddy OAuth `expiresIn`, `refreshToken`, refresh endpoint | **REFERENCE OBSERVED; RUNTIME UNVERIFIED** | Clean local `workbuddy_api` commit `d5de25a` defines `/v2/plugin/auth/state|token`, pending code `11217`, and reads `refreshToken`, `tokenType`, `expiresIn`, and `sessionState`. It does not establish a refresh endpoint or rotation behavior. |

## Reference commits (local)

| Project | Path | Commit (if available) |
| --- | --- | --- |
| workbuddy_api | `~/vibeCoding/workbuddy_api` | see design §2.2 (`d5de25a` at design time) |
| qoderwork-account-switcher | `~/vibeCoding/qoderwork-account-switcher` | see design §2.2 (`022c1d4` / v1.1.0 at design time) |

## Observed source evidence (2026-07-24)

- `qoderwork-account-switcher/src-tauri/src/core/quota.rs` at `022c1d4`
  declares the three Qoder endpoints and the flat `CLAIMED_TODAY|CLAIMABLE|DISABLED`
  status plus `CLAIMED|ALREADY_CLAIMED` claim shapes.
- The same file reads `token`, `refreshToken`, and `expiresAt` from decrypted
  `auth-v2.dat`; refresh sends `{"refresh_token": ...}` and accepts
  `device_token` before `token` as the access credential.
- Its Windows decrypt path reads `Local State` -> `os_crypt.encrypted_key`,
  removes the `DPAPI` prefix, calls user-scope DPAPI, then decrypts `v10` profile
  bytes with AES-256-GCM. The non-Windows path is an explicit error stub.
- `workbuddy_api/main.py` at `d5de25a` proves the OAuth/token response shape
  listed above, but searches of that clean checkout found no WorkBuddy
  `daily-checkin`, `checkin-status`, or `10001` implementation.

## Policy

- Production code comments/tests may cite these IDs.
- Do not mark a runtime fact **VERIFIED** until a redacted authorized spike records HTTP status, business code, requestId, and auth mode **without** headers/token bodies.
- Until verified: check-in purpose may store `verification_status=unverified`; scheduler should only run verified/active check-in slots.
