"""Account and credential write statements."""

UPSERT_ACCOUNT = """
INSERT INTO accounts (
    provider, account_id, label, source, enabled,
    masked_identity, identity_hash, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(provider, account_id) DO UPDATE SET
    label=excluded.label,
    source=excluded.source,
    enabled=excluded.enabled,
    masked_identity=excluded.masked_identity,
    identity_hash=excluded.identity_hash,
    updated_at=excluded.updated_at
"""

UPSERT_PURPOSE = """
INSERT INTO account_purposes (
    provider, account_id, purpose, enabled, status, verification_status,
    capabilities_json, verified_at, expires_at, last_success_at,
    failure_count, last_error, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(provider, account_id, purpose) DO UPDATE SET
    enabled=excluded.enabled,
    status=excluded.status,
    verification_status=excluded.verification_status,
    capabilities_json=excluded.capabilities_json,
    verified_at=excluded.verified_at,
    expires_at=excluded.expires_at,
    last_success_at=excluded.last_success_at,
    failure_count=excluded.failure_count,
    last_error=excluded.last_error,
    updated_at=excluded.updated_at
"""

INSERT_CREDENTIAL = """
INSERT INTO credentials (
    provider, account_id, purpose, mode, encrypted_payload,
    payload_version, credential_version, fingerprint_hmac,
    expires_at, has_refresh_token, updated_at
) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
"""

UPDATE_CREDENTIAL = """
UPDATE credentials SET
    mode=?, encrypted_payload=?, payload_version=?, credential_version=?,
    fingerprint_hmac=?, expires_at=?, has_refresh_token=?, updated_at=?
WHERE provider=? AND account_id=? AND purpose=? AND credential_version=?
"""
