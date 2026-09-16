package store

import (
	"context"
	"database/sql"
	"errors"
	"time"
)

// Session is one admin console session (only hashes are stored).
type Session struct {
	SessionHash string
	CSRFHash    string
	CreatedAt   string
	LastSeenAt  string
	ExpiresAt   string
	RevokedAt   *string
}

// CreateSession stores a new admin session.
func (d *DB) CreateSession(ctx context.Context, session Session) error {
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT INTO admin_sessions (session_hash, csrf_hash, created_at, last_seen_at, expires_at, revoked_at)
			VALUES (?, ?, ?, ?, ?, NULL)`,
			session.SessionHash, session.CSRFHash, session.CreatedAt, session.LastSeenAt, session.ExpiresAt)
		return err
	})
}

// GetSession loads a session by hash, including revoked rows so the caller can
// distinguish "expired" from "unknown".
func (d *DB) GetSession(ctx context.Context, sessionHash string) (Session, error) {
	var session Session
	err := d.QueryRowContext(ctx, `
		SELECT session_hash, csrf_hash, created_at, last_seen_at, expires_at, revoked_at
		  FROM admin_sessions WHERE session_hash=?`, sessionHash).
		Scan(&session.SessionHash, &session.CSRFHash, &session.CreatedAt, &session.LastSeenAt,
			&session.ExpiresAt, &session.RevokedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return Session{}, ErrNotFound
	}
	if err != nil {
		return Session{}, err
	}
	return session, nil
}

// TouchSession refreshes last_seen_at and extends the expiry.
func (d *DB) TouchSession(ctx context.Context, sessionHash, lastSeenAt, expiresAt string) error {
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx,
			"UPDATE admin_sessions SET last_seen_at=?, expires_at=? WHERE session_hash=?",
			lastSeenAt, expiresAt, sessionHash)
		return err
	})
}

// RevokeSession marks one session revoked.
func (d *DB) RevokeSession(ctx context.Context, sessionHash string) error {
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx,
			"UPDATE admin_sessions SET revoked_at=? WHERE session_hash=? AND revoked_at IS NULL",
			NowISO(), sessionHash)
		return err
	})
}

// PruneSessions deletes sessions that expired before the cutoff.
func (d *DB) PruneSessions(ctx context.Context, before string) error {
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, "DELETE FROM admin_sessions WHERE expires_at < ?", before)
		return err
	})
}

// ProxyKey is one row of proxy_api_keys.
type ProxyKey struct {
	KeyID      string   `json:"key_id"`
	Name       string   `json:"name"`
	KeyHash    string   `json:"-"`
	Scopes     []string `json:"scopes"`
	Enabled    bool     `json:"enabled"`
	CreatedAt  string   `json:"created_at"`
	LastUsedAt *string  `json:"last_used_at"`
	ExpiresAt  *string  `json:"expires_at"`
	RevokedAt  *string  `json:"revoked_at"`
}

// CreateProxyKey stores a new proxy key (hash only).
func (d *DB) CreateProxyKey(ctx context.Context, key ProxyKey) error {
	scopes, err := encodeScopes(key.Scopes)
	if err != nil {
		return err
	}
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT INTO proxy_api_keys (key_id, name, key_hash, scopes_json, enabled, created_at, expires_at, revoked_at)
			VALUES (?, ?, ?, ?, ?, ?, ?, NULL)`,
			key.KeyID, key.Name, key.KeyHash, scopes, boolInt(key.Enabled), key.CreatedAt, key.ExpiresAt)
		return err
	})
}

// ListProxyKeys returns every proxy key.
func (d *DB) ListProxyKeys(ctx context.Context) ([]ProxyKey, error) {
	rows, err := d.QueryContext(ctx, `
		SELECT key_id, name, key_hash, scopes_json, enabled, created_at, last_used_at, expires_at, revoked_at
		  FROM proxy_api_keys ORDER BY created_at DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ProxyKey
	for rows.Next() {
		var key ProxyKey
		var scopesJSON string
		var enabled int
		if err := rows.Scan(&key.KeyID, &key.Name, &key.KeyHash, &scopesJSON, &enabled,
			&key.CreatedAt, &key.LastUsedAt, &key.ExpiresAt, &key.RevokedAt); err != nil {
			return nil, err
		}
		key.Enabled = enabled != 0
		key.Scopes = decodeScopes(scopesJSON)
		out = append(out, key)
	}
	return out, rows.Err()
}

// ActiveProxyKeyHashes returns the digests the worker must accept.
func (d *DB) ActiveProxyKeyHashes(ctx context.Context, now time.Time) ([]string, error) {
	rows, err := d.QueryContext(ctx, `
		SELECT key_hash FROM proxy_api_keys
		 WHERE enabled=1 AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?)`, FormatISO(now))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var hash string
		if err := rows.Scan(&hash); err != nil {
			return nil, err
		}
		out = append(out, hash)
	}
	return out, rows.Err()
}

// RevokeProxyKey marks one key revoked.
func (d *DB) RevokeProxyKey(ctx context.Context, keyID string) error {
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx,
			"UPDATE proxy_api_keys SET revoked_at=? WHERE key_id=? AND revoked_at IS NULL", NowISO(), keyID)
		return err
	})
}

// DeleteProxyKey removes a key row.
func (d *DB) DeleteProxyKey(ctx context.Context, keyID string) error {
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, "DELETE FROM proxy_api_keys WHERE key_id=?", keyID)
		return err
	})
}

// AuditEvent is one row of audit_events.
type AuditEvent struct {
	EventID      string         `json:"event_id"`
	ActorType    string         `json:"actor_type"`
	ActorID      *string        `json:"actor_id"`
	Action       string         `json:"action"`
	ResourceType string         `json:"resource_type"`
	ResourceID   *string        `json:"resource_id"`
	Result       string         `json:"result"`
	Metadata     map[string]any `json:"metadata"`
	CreatedAt    string         `json:"created_at"`
}

// RecordAudit appends an audit event.
func (d *DB) RecordAudit(ctx context.Context, event AuditEvent) error {
	metadata, err := encodeJSON(event.Metadata)
	if err != nil {
		return err
	}
	if event.CreatedAt == "" {
		event.CreatedAt = NowISO()
	}
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT INTO audit_events
				(event_id, actor_type, actor_id, action, resource_type, resource_id, result, metadata_json, created_at)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
			event.EventID, orDefault(event.ActorType, "admin"), event.ActorID, event.Action,
			event.ResourceType, event.ResourceID, orDefault(event.Result, "succeeded"), metadata, event.CreatedAt)
		return err
	})
}

// AuditFilter narrows an audit query.
type AuditFilter struct {
	Search       string
	ActionPrefix string
	Category     string
	ResourceType string
	Result       string
}

// auditClause builds the shared WHERE fragment for audit queries.
func auditClause(filter AuditFilter) (string, []any) {
	clauses := []string{}
	args := []any{}
	if filter.ActionPrefix != "" {
		clauses = append(clauses, "action LIKE ?")
		args = append(args, filter.ActionPrefix+"%")
	}
	if filter.Category != "" {
		clauses = append(clauses, "action LIKE ?")
		args = append(args, filter.Category+".%")
	}
	if filter.ResourceType != "" {
		clauses = append(clauses, "resource_type=?")
		args = append(args, filter.ResourceType)
	}
	if filter.Result != "" {
		clauses = append(clauses, "result=?")
		args = append(args, filter.Result)
	}
	if filter.Search != "" {
		clauses = append(clauses, "(action LIKE ? OR COALESCE(resource_id,'') LIKE ? OR COALESCE(actor_id,'') LIKE ?)")
		pattern := "%" + filter.Search + "%"
		args = append(args, pattern, pattern, pattern)
	}
	if len(clauses) == 0 {
		return "1=1", args
	}
	return joinAnd(clauses), args
}

// ListAuditEvents returns audit rows newest-first.
func (d *DB) ListAuditEvents(ctx context.Context, filter AuditFilter, limit, offset int) ([]AuditEvent, error) {
	limit = clamp(limit, 1, 501)
	where, args := auditClause(filter)
	args = append(args, limit, offset)
	rows, err := d.QueryContext(ctx, `
		SELECT event_id, actor_type, actor_id, action, resource_type, resource_id, result, metadata_json, created_at
		  FROM audit_events WHERE `+where+`
		 ORDER BY created_at DESC, event_id DESC LIMIT ? OFFSET ?`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []AuditEvent
	for rows.Next() {
		var event AuditEvent
		var metadataJSON string
		if err := rows.Scan(&event.EventID, &event.ActorType, &event.ActorID, &event.Action,
			&event.ResourceType, &event.ResourceID, &event.Result, &metadataJSON, &event.CreatedAt); err != nil {
			return nil, err
		}
		event.Metadata = decodeMap(metadataJSON)
		out = append(out, event)
	}
	return out, rows.Err()
}

// CountAuditEvents counts rows matching a filter.
func (d *DB) CountAuditEvents(ctx context.Context, filter AuditFilter) (int, error) {
	where, args := auditClause(filter)
	var total int
	err := d.QueryRowContext(ctx, "SELECT COUNT(*) FROM audit_events WHERE "+where, args...).Scan(&total)
	return total, err
}

// RuntimeSetting is one row of runtime_settings.
type RuntimeSetting struct {
	Key         string  `json:"key"`
	Value       any     `json:"value"`
	ValueJSON   string  `json:"-"`
	Version     int     `json:"value_version"`
	Source      string  `json:"source"`
	ApplyMode   string  `json:"apply_mode"`
	ApplyStatus string  `json:"apply_status"`
	LastError   *string `json:"last_error"`
	UpdatedAt   string  `json:"updated_at"`
	UpdatedBy   *string `json:"updated_by"`
}

// ListRuntimeSettings returns every stored runtime override.
func (d *DB) ListRuntimeSettings(ctx context.Context) ([]RuntimeSetting, error) {
	rows, err := d.QueryContext(ctx, `
		SELECT key, value_json, value_version, source, apply_mode, apply_status, last_error, updated_at, updated_by
		  FROM runtime_settings ORDER BY key`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []RuntimeSetting
	for rows.Next() {
		var setting RuntimeSetting
		if err := rows.Scan(&setting.Key, &setting.ValueJSON, &setting.Version, &setting.Source,
			&setting.ApplyMode, &setting.ApplyStatus, &setting.LastError, &setting.UpdatedAt,
			&setting.UpdatedBy); err != nil {
			return nil, err
		}
		setting.Value = decodeAny(setting.ValueJSON)
		out = append(out, setting)
	}
	return out, rows.Err()
}

// PutRuntimeSetting writes one override, bumping its version.
func (d *DB) PutRuntimeSetting(ctx context.Context, key string, value any, applyMode, applyStatus, updatedBy string) (int, error) {
	encoded, err := encodeJSON(value)
	if err != nil {
		return 0, err
	}
	version := 0
	err = d.Write(ctx, func(tx *sql.Tx) error {
		var current int
		switch err := tx.QueryRowContext(ctx, "SELECT value_version FROM runtime_settings WHERE key=?", key).Scan(&current); {
		case err == nil:
			version = current + 1
		case errors.Is(err, sql.ErrNoRows):
			version = 1
		default:
			return err
		}
		_, err := tx.ExecContext(ctx, `
			INSERT INTO runtime_settings
				(key, value_json, value_version, source, apply_mode, apply_status, last_error, updated_at, updated_by)
			VALUES (?, ?, ?, 'runtime', ?, ?, NULL, ?, ?)
			ON CONFLICT(key) DO UPDATE SET
				value_json=excluded.value_json, value_version=excluded.value_version,
				source=excluded.source, apply_mode=excluded.apply_mode,
				apply_status=excluded.apply_status, last_error=NULL,
				updated_at=excluded.updated_at, updated_by=excluded.updated_by`,
			key, encoded, version, applyMode, applyStatus, NowISO(), updatedBy)
		return err
	})
	if err != nil {
		return 0, err
	}
	return version, nil
}

// RuntimeSettingsMap returns the overrides as a key→value map.
func (d *DB) RuntimeSettingsMap(ctx context.Context) (map[string]any, error) {
	settings, err := d.ListRuntimeSettings(ctx)
	if err != nil {
		return nil, err
	}
	out := make(map[string]any, len(settings))
	for _, setting := range settings {
		out[setting.Key] = setting.Value
	}
	return out, nil
}

// BackupRun is one row of backup_runs.
type BackupRun struct {
	BackupID      string  `json:"backup_id"`
	Path          string  `json:"path"`
	SchemaVersion string  `json:"schema_version"`
	StartedAt     string  `json:"started_at"`
	FinishedAt    *string `json:"finished_at"`
	Status        string  `json:"status"`
	SizeBytes     *int64  `json:"size_bytes"`
	SHA256        *string `json:"sha256"`
	ErrorMessage  *string `json:"error_message"`
}

// RecordBackupRun stores a backup record.
func (d *DB) RecordBackupRun(ctx context.Context, run BackupRun) error {
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT INTO backup_runs
				(backup_id, path, schema_version, started_at, finished_at, status, size_bytes, sha256, error_message)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(backup_id) DO UPDATE SET
				finished_at=excluded.finished_at, status=excluded.status, size_bytes=excluded.size_bytes,
				sha256=excluded.sha256, error_message=excluded.error_message`,
			run.BackupID, run.Path, run.SchemaVersion, run.StartedAt, run.FinishedAt, run.Status,
			run.SizeBytes, run.SHA256, run.ErrorMessage)
		return err
	})
}

// ListBackupRuns returns backup records newest-first.
func (d *DB) ListBackupRuns(ctx context.Context, status string, limit, offset int) ([]BackupRun, error) {
	limit = clamp(limit, 1, 501)
	query := `SELECT backup_id, path, schema_version, started_at, finished_at, status, size_bytes, sha256, error_message
	            FROM backup_runs`
	args := []any{}
	if status != "" {
		query += " WHERE status=?"
		args = append(args, status)
	}
	query += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
	args = append(args, limit, offset)
	rows, err := d.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []BackupRun
	for rows.Next() {
		var run BackupRun
		if err := rows.Scan(&run.BackupID, &run.Path, &run.SchemaVersion, &run.StartedAt,
			&run.FinishedAt, &run.Status, &run.SizeBytes, &run.SHA256, &run.ErrorMessage); err != nil {
			return nil, err
		}
		out = append(out, run)
	}
	return out, rows.Err()
}

// ServiceEvent is one row of service_events.
type ServiceEvent struct {
	Cursor        int64   `json:"cursor"`
	EventID       string  `json:"event_id"`
	ServiceName   string  `json:"service_name"`
	EventType     string  `json:"event_type"`
	Action        *string `json:"action"`
	DesiredState  *string `json:"desired_state"`
	ObservedState *string `json:"observed_state"`
	OperationID   *string `json:"operation_id"`
	Status        *string `json:"status"`
	InFlight      *int    `json:"in_flight"`
	ErrorCode     *string `json:"error_code"`
	CreatedAt     string  `json:"created_at"`
}

// RecordServiceEvent appends a service lifecycle event.
func (d *DB) RecordServiceEvent(ctx context.Context, event ServiceEvent) error {
	if event.CreatedAt == "" {
		event.CreatedAt = NowISO()
	}
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT OR IGNORE INTO service_events
				(event_id, service_name, event_type, action, desired_state, observed_state, operation_id,
				 status, in_flight, error_code, created_at)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
			event.EventID, event.ServiceName, event.EventType, event.Action, event.DesiredState,
			event.ObservedState, event.OperationID, event.Status, event.InFlight, event.ErrorCode,
			event.CreatedAt)
		return err
	})
}

// ListServiceEvents returns service events newest-first.
func (d *DB) ListServiceEvents(ctx context.Context, eventType, status string, limit, offset int) ([]ServiceEvent, error) {
	limit = clamp(limit, 1, 501)
	query := `SELECT cursor, event_id, service_name, event_type, action, desired_state, observed_state,
	                 operation_id, status, in_flight, error_code, created_at FROM service_events`
	clauses := []string{}
	args := []any{}
	if eventType != "" {
		clauses = append(clauses, "event_type=?")
		args = append(args, eventType)
	}
	if status != "" {
		clauses = append(clauses, "status=?")
		args = append(args, status)
	}
	if len(clauses) > 0 {
		query += " WHERE " + joinAnd(clauses)
	}
	query += " ORDER BY cursor DESC LIMIT ? OFFSET ?"
	args = append(args, limit, offset)
	rows, err := d.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ServiceEvent
	for rows.Next() {
		var event ServiceEvent
		if err := rows.Scan(&event.Cursor, &event.EventID, &event.ServiceName, &event.EventType,
			&event.Action, &event.DesiredState, &event.ObservedState, &event.OperationID,
			&event.Status, &event.InFlight, &event.ErrorCode, &event.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, event)
	}
	return out, rows.Err()
}

// ServiceRuntime is the single row describing the service state.
type ServiceRuntime struct {
	ServiceName     string   `json:"service_name"`
	DesiredState    string   `json:"desired_state"`
	ObservedState   string   `json:"observed_state"`
	WorkerPID       *int     `json:"worker_pid"`
	ProcessStart    *float64 `json:"process_start_time"`
	ProcessGroupID  *int     `json:"process_group_id"`
	OwnerInstanceID *string  `json:"owner_instance_id"`
	InternalAuth    *int     `json:"internal_auth_version"`
	StartedAt       *string  `json:"started_at"`
	StoppedAt       *string  `json:"stopped_at"`
	LastHealthAt    *string  `json:"last_health_at"`
	LastExitCode    *int     `json:"last_exit_code"`
	LastError       *string  `json:"last_error"`
	UpdatedAt       string   `json:"updated_at"`
}

// GetServiceRuntime loads the service runtime row, if present.
func (d *DB) GetServiceRuntime(ctx context.Context, serviceName string) (ServiceRuntime, error) {
	var runtime ServiceRuntime
	err := d.QueryRowContext(ctx, `
		SELECT service_name, desired_state, observed_state, worker_pid, process_start_time, process_group_id,
		       owner_instance_id, internal_auth_version, started_at, stopped_at, last_health_at,
		       last_exit_code, last_error, updated_at
		  FROM service_runtime WHERE service_name=?`, serviceName).
		Scan(&runtime.ServiceName, &runtime.DesiredState, &runtime.ObservedState, &runtime.WorkerPID,
			&runtime.ProcessStart, &runtime.ProcessGroupID, &runtime.OwnerInstanceID, &runtime.InternalAuth,
			&runtime.StartedAt, &runtime.StoppedAt, &runtime.LastHealthAt, &runtime.LastExitCode,
			&runtime.LastError, &runtime.UpdatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return ServiceRuntime{}, ErrNotFound
	}
	if err != nil {
		return ServiceRuntime{}, err
	}
	return runtime, nil
}

// PutServiceRuntime upserts the service runtime row.
func (d *DB) PutServiceRuntime(ctx context.Context, runtime ServiceRuntime) error {
	if runtime.UpdatedAt == "" {
		runtime.UpdatedAt = NowISO()
	}
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT INTO service_runtime
				(service_name, desired_state, observed_state, worker_pid, process_start_time, process_group_id,
				 owner_instance_id, internal_auth_version, started_at, stopped_at, last_health_at,
				 last_exit_code, last_error, updated_at)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(service_name) DO UPDATE SET
				desired_state=excluded.desired_state, observed_state=excluded.observed_state,
				worker_pid=excluded.worker_pid, process_start_time=excluded.process_start_time,
				process_group_id=excluded.process_group_id, owner_instance_id=excluded.owner_instance_id,
				internal_auth_version=excluded.internal_auth_version, started_at=excluded.started_at,
				stopped_at=excluded.stopped_at, last_health_at=excluded.last_health_at,
				last_exit_code=excluded.last_exit_code, last_error=excluded.last_error,
				updated_at=excluded.updated_at`,
			runtime.ServiceName, runtime.DesiredState, runtime.ObservedState, runtime.WorkerPID,
			runtime.ProcessStart, runtime.ProcessGroupID, runtime.OwnerInstanceID, runtime.InternalAuth,
			runtime.StartedAt, runtime.StoppedAt, runtime.LastHealthAt, runtime.LastExitCode,
			runtime.LastError, runtime.UpdatedAt)
		return err
	})
}

// ServiceOperation is one row of service_operations.
type ServiceOperation struct {
	OperationID string  `json:"operation_id"`
	ServiceName string  `json:"service_name"`
	Action      string  `json:"action"`
	Status      string  `json:"status"`
	Error       *string `json:"error"`
	CreatedAt   string  `json:"created_at"`
	FinishedAt  *string `json:"finished_at"`
}

// PutServiceOperation upserts a service operation row.
func (d *DB) PutServiceOperation(ctx context.Context, operation ServiceOperation) error {
	if operation.CreatedAt == "" {
		operation.CreatedAt = NowISO()
	}
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT INTO service_operations (operation_id, service_name, action, status, error, created_at, finished_at)
			VALUES (?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(operation_id) DO UPDATE SET
				status=excluded.status, error=excluded.error, finished_at=excluded.finished_at`,
			operation.OperationID, operation.ServiceName, operation.Action, operation.Status,
			operation.Error, operation.CreatedAt, operation.FinishedAt)
		return err
	})
}

// GetServiceOperation loads one operation row.
func (d *DB) GetServiceOperation(ctx context.Context, operationID string) (ServiceOperation, error) {
	var operation ServiceOperation
	err := d.QueryRowContext(ctx, `
		SELECT operation_id, service_name, action, status, error, created_at, finished_at
		  FROM service_operations WHERE operation_id=?`, operationID).
		Scan(&operation.OperationID, &operation.ServiceName, &operation.Action, &operation.Status,
			&operation.Error, &operation.CreatedAt, &operation.FinishedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return ServiceOperation{}, ErrNotFound
	}
	if err != nil {
		return ServiceOperation{}, err
	}
	return operation, nil
}

func joinAnd(clauses []string) string {
	out := ""
	for index, clause := range clauses {
		if index > 0 {
			out += " AND "
		}
		out += clause
	}
	return out
}
