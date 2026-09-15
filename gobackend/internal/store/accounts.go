package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
)

// ErrNotFound is returned when a single-row lookup matches nothing.
var ErrNotFound = errors.New("not found")

// VersionConflict means an optimistic-concurrency check failed: another writer
// rotated the credential first.
var VersionConflict = errors.New("credential version conflict")

// Account is one row of the accounts table.
type Account struct {
	Provider       string  `json:"provider"`
	AccountID      string  `json:"account_id"`
	Label          string  `json:"label"`
	Source         string  `json:"source"`
	Enabled        bool    `json:"enabled"`
	MaskedIdentity *string `json:"masked_identity"`
	IdentityHash   *string `json:"identity_hash"`
	CreatedAt      string  `json:"created_at"`
	UpdatedAt      string  `json:"updated_at"`
}

// Purpose is one row of the account_purposes table.
type Purpose struct {
	Provider           string   `json:"provider"`
	AccountID          string   `json:"account_id"`
	Purpose            string   `json:"purpose"`
	Enabled            bool     `json:"enabled"`
	Status             string   `json:"status"`
	VerificationStatus string   `json:"verification_status"`
	Capabilities       []string `json:"capabilities"`
	VerifiedAt         *string  `json:"verified_at"`
	ExpiresAt          *string  `json:"expires_at"`
	LastSuccessAt      *string  `json:"last_success_at"`
	FailureCount       int      `json:"failure_count"`
	LastError          *string  `json:"last_error"`
	UpdatedAt          string   `json:"updated_at"`
}

// Credential is one row of the credentials table, payload excluded.
type Credential struct {
	Provider          string  `json:"provider"`
	AccountID         string  `json:"account_id"`
	Purpose           string  `json:"purpose"`
	Mode              string  `json:"mode"`
	PayloadVersion    int     `json:"payload_version"`
	CredentialVersion int     `json:"credential_version"`
	FingerprintHMAC   *string `json:"-"`
	ExpiresAt         *string `json:"expires_at"`
	HasRefreshToken   bool    `json:"has_refresh_token"`
	UpdatedAt         string  `json:"updated_at"`
}

// UpsertAccount inserts or updates the account row and returns it.
func (d *DB) UpsertAccount(ctx context.Context, account Account) (Account, error) {
	now := NowISO()
	err := d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT INTO accounts (provider, account_id, label, source, enabled, masked_identity, identity_hash, created_at, updated_at)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(provider, account_id) DO UPDATE SET
				label=excluded.label, source=excluded.source, enabled=excluded.enabled,
				masked_identity=excluded.masked_identity, identity_hash=excluded.identity_hash,
				updated_at=excluded.updated_at`,
			account.Provider, account.AccountID, account.Label, orDefault(account.Source, "manual"),
			boolInt(account.Enabled), account.MaskedIdentity, account.IdentityHash, now, now)
		return err
	})
	if err != nil {
		return Account{}, err
	}
	stored, err := d.GetAccount(ctx, account.Provider, account.AccountID)
	if err != nil {
		return Account{}, err
	}
	return stored, nil
}

// GetAccount loads one account row.
func (d *DB) GetAccount(ctx context.Context, provider, accountID string) (Account, error) {
	var account Account
	var enabled int
	err := d.QueryRowContext(ctx,
		`SELECT provider, account_id, label, source, enabled, masked_identity, identity_hash, created_at, updated_at
		   FROM accounts WHERE provider=? AND account_id=?`, provider, accountID).
		Scan(&account.Provider, &account.AccountID, &account.Label, &account.Source, &enabled,
			&account.MaskedIdentity, &account.IdentityHash, &account.CreatedAt, &account.UpdatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return Account{}, ErrNotFound
	}
	if err != nil {
		return Account{}, err
	}
	account.Enabled = enabled != 0
	return account, nil
}

// ListAccounts returns accounts ordered by provider then account id. When
// providers is non-empty the result is filtered to those providers.
func (d *DB) ListAccounts(ctx context.Context, providers []string) ([]Account, error) {
	query := `SELECT provider, account_id, label, source, enabled, masked_identity, identity_hash, created_at, updated_at
	            FROM accounts`
	args := []any{}
	if len(providers) > 0 {
		query += " WHERE provider IN (" + placeholders(len(providers)) + ")"
		for _, provider := range providers {
			args = append(args, provider)
		}
	}
	query += " ORDER BY provider, account_id"
	rows, err := d.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Account
	for rows.Next() {
		var account Account
		var enabled int
		if err := rows.Scan(&account.Provider, &account.AccountID, &account.Label, &account.Source, &enabled,
			&account.MaskedIdentity, &account.IdentityHash, &account.CreatedAt, &account.UpdatedAt); err != nil {
			return nil, err
		}
		account.Enabled = enabled != 0
		out = append(out, account)
	}
	return out, rows.Err()
}

// UpdateAccountFields patches the mutable account columns.
func (d *DB) UpdateAccountFields(ctx context.Context, provider, accountID string, enabled *bool, label *string) error {
	return d.Write(ctx, func(tx *sql.Tx) error {
		sets := []string{"updated_at=?"}
		args := []any{NowISO()}
		if enabled != nil {
			sets = append(sets, "enabled=?")
			args = append(args, boolInt(*enabled))
		}
		if label != nil {
			sets = append(sets, "label=?")
			args = append(args, *label)
		}
		args = append(args, provider, accountID)
		_, err := tx.ExecContext(ctx,
			"UPDATE accounts SET "+strings.Join(sets, ", ")+" WHERE provider=? AND account_id=?", args...)
		return err
	})
}

// DeleteAccount removes the account, its purposes and its credentials.
func (d *DB) DeleteAccount(ctx context.Context, provider, accountID string) error {
	return d.Write(ctx, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, "DELETE FROM credentials WHERE provider=? AND account_id=?", provider, accountID); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, "DELETE FROM account_purposes WHERE provider=? AND account_id=?", provider, accountID); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, "DELETE FROM account_model_blocks WHERE provider=? AND account_id=?", provider, accountID); err != nil {
			return err
		}
		_, err := tx.ExecContext(ctx, "DELETE FROM accounts WHERE provider=? AND account_id=?", provider, accountID)
		return err
	})
}

// UpsertPurpose inserts or updates an account purpose row.
func (d *DB) UpsertPurpose(ctx context.Context, purpose Purpose) error {
	capabilities, err := json.Marshal(orEmptySlice(purpose.Capabilities))
	if err != nil {
		return err
	}
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT INTO account_purposes
				(provider, account_id, purpose, enabled, status, verification_status, capabilities_json,
				 verified_at, expires_at, last_success_at, failure_count, last_error, updated_at)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(provider, account_id, purpose) DO UPDATE SET
				enabled=excluded.enabled, status=excluded.status,
				verification_status=excluded.verification_status, capabilities_json=excluded.capabilities_json,
				verified_at=excluded.verified_at, expires_at=excluded.expires_at,
				last_success_at=excluded.last_success_at, failure_count=excluded.failure_count,
				last_error=excluded.last_error, updated_at=excluded.updated_at`,
			purpose.Provider, purpose.AccountID, purpose.Purpose, boolInt(purpose.Enabled),
			orDefault(purpose.Status, "unconfigured"), orDefault(purpose.VerificationStatus, "unverified"),
			string(capabilities), purpose.VerifiedAt, purpose.ExpiresAt, purpose.LastSuccessAt,
			purpose.FailureCount, purpose.LastError, NowISO())
		return err
	})
}

// SetPurposeExpiry mirrors a rotated credential's deadline onto the purpose row
// so the admin account view stops advertising the stale one. The caller is
// expected to treat a failure here as cosmetic.
func (d *DB) SetPurposeExpiry(ctx context.Context, provider, accountID, purpose string, expiresAt *string) error {
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx,
			"UPDATE account_purposes SET expires_at=?, updated_at=? WHERE provider=? AND account_id=? AND purpose=?",
			expiresAt, NowISO(), provider, accountID, purpose)
		return err
	})
}

// ListPurposes returns the purpose rows for one account.
func (d *DB) ListPurposes(ctx context.Context, provider, accountID string) ([]Purpose, error) {
	rows, err := d.QueryContext(ctx, `
		SELECT provider, account_id, purpose, enabled, status, verification_status, capabilities_json,
		       verified_at, expires_at, last_success_at, failure_count, last_error, updated_at
		  FROM account_purposes WHERE provider=? AND account_id=? ORDER BY purpose`, provider, accountID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanPurposes(rows)
}

// ListAllPurposes returns every purpose row.
func (d *DB) ListAllPurposes(ctx context.Context) ([]Purpose, error) {
	rows, err := d.QueryContext(ctx, `
		SELECT provider, account_id, purpose, enabled, status, verification_status, capabilities_json,
		       verified_at, expires_at, last_success_at, failure_count, last_error, updated_at
		  FROM account_purposes ORDER BY provider, account_id, purpose`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanPurposes(rows)
}

func scanPurposes(rows *sql.Rows) ([]Purpose, error) {
	var out []Purpose
	for rows.Next() {
		var purpose Purpose
		var enabled int
		var capabilitiesJSON string
		if err := rows.Scan(&purpose.Provider, &purpose.AccountID, &purpose.Purpose, &enabled,
			&purpose.Status, &purpose.VerificationStatus, &capabilitiesJSON, &purpose.VerifiedAt,
			&purpose.ExpiresAt, &purpose.LastSuccessAt, &purpose.FailureCount, &purpose.LastError,
			&purpose.UpdatedAt); err != nil {
			return nil, err
		}
		purpose.Enabled = enabled != 0
		purpose.Capabilities = []string{}
		_ = json.Unmarshal([]byte(capabilitiesJSON), &purpose.Capabilities)
		out = append(out, purpose)
	}
	return out, rows.Err()
}

// CredentialRecord is a stored credential including its encrypted payload.
type CredentialRecord struct {
	Credential
	EncryptedPayload string
}

// GetCredential loads one credential row with its encrypted payload.
func (d *DB) GetCredential(ctx context.Context, provider, accountID, purpose string) (CredentialRecord, error) {
	var record CredentialRecord
	err := d.QueryRowContext(ctx, `
		SELECT provider, account_id, purpose, mode, encrypted_payload, payload_version, credential_version,
		       fingerprint_hmac, expires_at, has_refresh_token, updated_at
		  FROM credentials WHERE provider=? AND account_id=? AND purpose=?`,
		provider, accountID, purpose).
		Scan(&record.Provider, &record.AccountID, &record.Purpose, &record.Mode, &record.EncryptedPayload,
			&record.PayloadVersion, &record.CredentialVersion, &record.FingerprintHMAC, &record.ExpiresAt,
			&record.HasRefreshToken, &record.UpdatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return CredentialRecord{}, ErrNotFound
	}
	if err != nil {
		return CredentialRecord{}, err
	}
	return record, nil
}

// CredentialWrite is the input for an upsert.
type CredentialWrite struct {
	Provider         string
	AccountID        string
	Purpose          string
	Mode             string
	EncryptedPayload string
	FingerprintHMAC  *string
	ExpiresAt        *string
	HasRefreshToken  bool
	ExpectedVersion  *int
}

// UpsertCredential stores a credential, bumping credential_version. When
// ExpectedVersion is set the write is rejected with VersionConflict if another
// writer already rotated the row.
func (d *DB) UpsertCredential(ctx context.Context, write CredentialWrite) (int, error) {
	version := 0
	err := d.Write(ctx, func(tx *sql.Tx) error {
		var current *int
		var currentVersion int
		row := tx.QueryRowContext(ctx,
			"SELECT credential_version FROM credentials WHERE provider=? AND account_id=? AND purpose=?",
			write.Provider, write.AccountID, write.Purpose)
		switch err := row.Scan(&currentVersion); {
		case err == nil:
			current = &currentVersion
		case errors.Is(err, sql.ErrNoRows):
		default:
			return err
		}
		if write.ExpectedVersion != nil && current != nil && *current != *write.ExpectedVersion {
			return VersionConflict
		}
		version = 1
		if current != nil {
			version = *current + 1
		}
		_, err := tx.ExecContext(ctx, `
			INSERT INTO credentials
				(provider, account_id, purpose, mode, encrypted_payload, payload_version, credential_version,
				 fingerprint_hmac, expires_at, has_refresh_token, updated_at)
			VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
			ON CONFLICT(provider, account_id, purpose) DO UPDATE SET
				mode=excluded.mode, encrypted_payload=excluded.encrypted_payload,
				credential_version=excluded.credential_version, fingerprint_hmac=excluded.fingerprint_hmac,
				expires_at=excluded.expires_at, has_refresh_token=excluded.has_refresh_token,
				updated_at=excluded.updated_at`,
			write.Provider, write.AccountID, write.Purpose, orDefault(write.Mode, "bearer"),
			write.EncryptedPayload, version, write.FingerprintHMAC, write.ExpiresAt,
			boolInt(write.HasRefreshToken), NowISO())
		return err
	})
	if err != nil {
		return 0, err
	}
	return version, nil
}

// ListCredentials returns credential metadata (no payload) for the providers given.
func (d *DB) ListCredentials(ctx context.Context, providers []string) ([]Credential, error) {
	query := `SELECT provider, account_id, purpose, mode, payload_version, credential_version,
	                 fingerprint_hmac, expires_at, has_refresh_token, updated_at FROM credentials`
	args := []any{}
	if len(providers) > 0 {
		query += " WHERE provider IN (" + placeholders(len(providers)) + ")"
		for _, provider := range providers {
			args = append(args, provider)
		}
	}
	query += " ORDER BY provider, account_id, purpose"
	rows, err := d.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Credential
	for rows.Next() {
		var credential Credential
		if err := rows.Scan(&credential.Provider, &credential.AccountID, &credential.Purpose,
			&credential.Mode, &credential.PayloadVersion, &credential.CredentialVersion,
			&credential.FingerprintHMAC, &credential.ExpiresAt, &credential.HasRefreshToken,
			&credential.UpdatedAt); err != nil {
			return nil, err
		}
		out = append(out, credential)
	}
	return out, rows.Err()
}

// DeleteCredential removes one stored credential.
func (d *DB) DeleteCredential(ctx context.Context, provider, accountID, purpose string) error {
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx,
			"DELETE FROM credentials WHERE provider=? AND account_id=? AND purpose=?",
			provider, accountID, purpose)
		return err
	})
}

// FindAccountByIdentityHash locates an account by its identity fingerprint, used
// to keep one row per signed-in identity.
func (d *DB) FindAccountByIdentityHash(ctx context.Context, provider, identityHash string) (Account, error) {
	var account Account
	var enabled int
	err := d.QueryRowContext(ctx, `
		SELECT provider, account_id, label, source, enabled, masked_identity, identity_hash, created_at, updated_at
		  FROM accounts WHERE provider=? AND identity_hash=? LIMIT 1`, provider, identityHash).
		Scan(&account.Provider, &account.AccountID, &account.Label, &account.Source, &enabled,
			&account.MaskedIdentity, &account.IdentityHash, &account.CreatedAt, &account.UpdatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return Account{}, ErrNotFound
	}
	if err != nil {
		return Account{}, err
	}
	account.Enabled = enabled != 0
	return account, nil
}

func orDefault(value, fallback string) string {
	if strings.TrimSpace(value) == "" {
		return fallback
	}
	return value
}

func orEmptySlice(values []string) []string {
	if values == nil {
		return []string{}
	}
	return values
}

func boolInt(value bool) int {
	if value {
		return 1
	}
	return 0
}

// FormatError wraps an error with context for the caller's log line.
func FormatError(action string, err error) error {
	return fmt.Errorf("%s: %w", action, err)
}
