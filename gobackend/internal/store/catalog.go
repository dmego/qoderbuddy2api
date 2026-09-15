package store

import (
	"context"
	"database/sql"
	"errors"
)

// CatalogModel is one row of model_catalog.
type CatalogModel struct {
	Provider     string         `json:"provider"`
	ModelID      string         `json:"model_id"`
	DisplayName  string         `json:"display_name"`
	Capabilities []string       `json:"capabilities"`
	Source       string         `json:"source"`
	Enabled      bool           `json:"enabled"`
	LastSeenAt   string         `json:"last_seen_at"`
	Metadata     map[string]any `json:"metadata"`
}

// ListCatalogModels returns catalog rows for the providers given (all if empty).
func (d *DB) ListCatalogModels(ctx context.Context, providers []string) ([]CatalogModel, error) {
	query := `SELECT provider, model_id, display_name, capabilities_json, source, enabled, last_seen_at, metadata_json
	            FROM model_catalog`
	args := []any{}
	if len(providers) > 0 {
		query += " WHERE provider IN (" + placeholders(len(providers)) + ")"
		for _, provider := range providers {
			args = append(args, provider)
		}
	}
	query += " ORDER BY provider, model_id"
	rows, err := d.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []CatalogModel
	for rows.Next() {
		var model CatalogModel
		var capabilitiesJSON, metadataJSON string
		var enabled int
		if err := rows.Scan(&model.Provider, &model.ModelID, &model.DisplayName, &capabilitiesJSON,
			&model.Source, &enabled, &model.LastSeenAt, &metadataJSON); err != nil {
			return nil, err
		}
		model.Enabled = enabled != 0
		model.Capabilities = decodeScopes(capabilitiesJSON)
		model.Metadata = decodeMap(metadataJSON)
		out = append(out, model)
	}
	return out, rows.Err()
}

// EnabledCatalogModels maps model id → enabled for one provider.
func (d *DB) EnabledCatalogModels(ctx context.Context, provider string) (map[string]bool, error) {
	rows, err := d.QueryContext(ctx, "SELECT model_id, enabled FROM model_catalog WHERE provider=?", provider)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]bool{}
	for rows.Next() {
		var modelID string
		var enabled int
		if err := rows.Scan(&modelID, &enabled); err != nil {
			return nil, err
		}
		out[modelID] = enabled != 0
	}
	return out, rows.Err()
}

// UpsertCatalogModel inserts or updates a catalog row.
func (d *DB) UpsertCatalogModel(ctx context.Context, model CatalogModel) error {
	capabilities, err := encodeJSON(orDefaultSlice(model.Capabilities))
	if err != nil {
		return err
	}
	metadata, err := encodeJSON(orDefaultMap(model.Metadata))
	if err != nil {
		return err
	}
	if model.LastSeenAt == "" {
		model.LastSeenAt = NowISO()
	}
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT INTO model_catalog
				(provider, model_id, display_name, capabilities_json, source, enabled, last_seen_at, metadata_json)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(provider, model_id) DO UPDATE SET
				display_name=excluded.display_name, capabilities_json=excluded.capabilities_json,
				source=excluded.source, enabled=excluded.enabled, last_seen_at=excluded.last_seen_at,
				metadata_json=excluded.metadata_json`,
			model.Provider, model.ModelID, model.DisplayName, capabilities,
			orDefault(model.Source, "definition"), boolInt(model.Enabled), model.LastSeenAt, metadata)
		return err
	})
}

// SetCatalogModelEnabled flips the enabled flag for one catalog row.
func (d *DB) SetCatalogModelEnabled(ctx context.Context, provider, modelID string, enabled bool) (int, error) {
	affected := 0
	err := d.Write(ctx, func(tx *sql.Tx) error {
		result, err := tx.ExecContext(ctx,
			"UPDATE model_catalog SET enabled=? WHERE provider=? AND model_id=?",
			boolInt(enabled), provider, modelID)
		if err != nil {
			return err
		}
		count, err := result.RowsAffected()
		if err == nil {
			affected = int(count)
		}
		return nil
	})
	return affected, err
}

// RoutePolicyRow is one row of model_route_policies.
type RoutePolicyRow struct {
	ModelID   string `json:"model_id"`
	Provider  string `json:"provider"`
	Priority  int    `json:"priority"`
	Weight    int    `json:"weight"`
	Enabled   bool   `json:"enabled"`
	UpdatedAt string `json:"updated_at"`
}

// ListRoutePolicies returns every stored route policy.
func (d *DB) ListRoutePolicies(ctx context.Context) ([]RoutePolicyRow, error) {
	rows, err := d.QueryContext(ctx, `
		SELECT model_id, provider, priority, weight, enabled, updated_at
		  FROM model_route_policies ORDER BY model_id, priority, provider`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []RoutePolicyRow
	for rows.Next() {
		var policy RoutePolicyRow
		var enabled int
		if err := rows.Scan(&policy.ModelID, &policy.Provider, &policy.Priority, &policy.Weight,
			&enabled, &policy.UpdatedAt); err != nil {
			return nil, err
		}
		policy.Enabled = enabled != 0
		out = append(out, policy)
	}
	return out, rows.Err()
}

// ReplaceRoutePolicies swaps the policy set for one model in a single write.
func (d *DB) ReplaceRoutePolicies(ctx context.Context, modelID string, policies []RoutePolicyRow) error {
	return d.Write(ctx, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, "DELETE FROM model_route_policies WHERE model_id=?", modelID); err != nil {
			return err
		}
		for _, policy := range policies {
			if _, err := tx.ExecContext(ctx, `
				INSERT INTO model_route_policies (model_id, provider, priority, weight, enabled, updated_at)
				VALUES (?, ?, ?, ?, ?, ?)`,
				modelID, policy.Provider, policy.Priority, policy.Weight, boolInt(policy.Enabled), NowISO()); err != nil {
				return err
			}
		}
		return nil
	})
}

// DeleteRoutePolicies removes every policy row for one model.
func (d *DB) DeleteRoutePolicies(ctx context.Context, modelID string) error {
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, "DELETE FROM model_route_policies WHERE model_id=?", modelID)
		return err
	})
}

// AccountModelBlock is one row of account_model_blocks.
type AccountModelBlock struct {
	Provider     string  `json:"provider"`
	AccountID    string  `json:"account_id"`
	ModelID      string  `json:"model_id"`
	Reason       string  `json:"reason"`
	Source       string  `json:"source"`
	BlockedUntil *string `json:"blocked_until"`
	UpdatedAt    string  `json:"updated_at"`
}

// ListAccountModelBlocks returns the manual per-account exclusions.
func (d *DB) ListAccountModelBlocks(ctx context.Context) ([]AccountModelBlock, error) {
	rows, err := d.QueryContext(ctx, `
		SELECT provider, account_id, model_id, reason, source, blocked_until, updated_at
		  FROM account_model_blocks ORDER BY provider, account_id, model_id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []AccountModelBlock
	for rows.Next() {
		var block AccountModelBlock
		if err := rows.Scan(&block.Provider, &block.AccountID, &block.ModelID, &block.Reason,
			&block.Source, &block.BlockedUntil, &block.UpdatedAt); err != nil {
			return nil, err
		}
		out = append(out, block)
	}
	return out, rows.Err()
}

// UpsertAccountModelBlock stores one manual exclusion.
func (d *DB) UpsertAccountModelBlock(ctx context.Context, block AccountModelBlock) error {
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT INTO account_model_blocks
				(provider, account_id, model_id, reason, source, blocked_until, updated_at)
			VALUES (?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(provider, account_id, model_id) DO UPDATE SET
				reason=excluded.reason, source=excluded.source,
				blocked_until=excluded.blocked_until, updated_at=excluded.updated_at`,
			block.Provider, block.AccountID, block.ModelID, block.Reason,
			orDefault(block.Source, "manual"), block.BlockedUntil, NowISO())
		return err
	})
}

// DeleteAccountModelBlock removes one exclusion.
func (d *DB) DeleteAccountModelBlock(ctx context.Context, provider, accountID, modelID string) error {
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx,
			"DELETE FROM account_model_blocks WHERE provider=? AND account_id=? AND model_id=?",
			provider, accountID, modelID)
		return err
	})
}

// MetricSnapshot is one row of account_metric_snapshots.
type MetricSnapshot struct {
	Provider   string  `json:"provider"`
	AccountID  string  `json:"account_id"`
	MetricKind string  `json:"metric_kind"`
	Value      any     `json:"value"`
	ObservedAt string  `json:"observed_at"`
	ExpiresAt  *string `json:"expires_at"`
	Status     string  `json:"status"`
	LastError  *string `json:"last_error"`
}

// UpsertMetricSnapshot writes the latest value for (account, kind).
func (d *DB) UpsertMetricSnapshot(ctx context.Context, snapshot MetricSnapshot) error {
	value, err := encodeJSON(snapshot.Value)
	if err != nil {
		return err
	}
	if snapshot.ObservedAt == "" {
		snapshot.ObservedAt = NowISO()
	}
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT INTO account_metric_snapshots
				(provider, account_id, metric_kind, metric_value_json, observed_at, expires_at, status, last_error)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(provider, account_id, metric_kind) DO UPDATE SET
				metric_value_json=excluded.metric_value_json, observed_at=excluded.observed_at,
				expires_at=excluded.expires_at, status=excluded.status, last_error=excluded.last_error`,
			snapshot.Provider, snapshot.AccountID, snapshot.MetricKind, value, snapshot.ObservedAt,
			snapshot.ExpiresAt, orDefault(snapshot.Status, "fresh"), snapshot.LastError)
		return err
	})
}

// ListMetricSnapshots returns snapshots filtered by provider and/or account.
func (d *DB) ListMetricSnapshots(ctx context.Context, provider, accountID string) ([]MetricSnapshot, error) {
	query := `SELECT provider, account_id, metric_kind, metric_value_json, observed_at, expires_at, status, last_error
	            FROM account_metric_snapshots`
	clauses := []string{}
	args := []any{}
	if provider != "" {
		clauses = append(clauses, "provider=?")
		args = append(args, provider)
	}
	if accountID != "" {
		clauses = append(clauses, "account_id=?")
		args = append(args, accountID)
	}
	if len(clauses) > 0 {
		query += " WHERE " + joinAnd(clauses)
	}
	query += " ORDER BY provider, account_id, metric_kind"
	rows, err := d.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanMetricSnapshots(rows)
}

func scanMetricSnapshots(rows *sql.Rows) ([]MetricSnapshot, error) {
	var out []MetricSnapshot
	for rows.Next() {
		var snapshot MetricSnapshot
		var valueJSON string
		if err := rows.Scan(&snapshot.Provider, &snapshot.AccountID, &snapshot.MetricKind, &valueJSON,
			&snapshot.ObservedAt, &snapshot.ExpiresAt, &snapshot.Status, &snapshot.LastError); err != nil {
			return nil, err
		}
		snapshot.Value = decodeAny(valueJSON)
		out = append(out, snapshot)
	}
	return out, rows.Err()
}

// MetricHistoryRow is one row of account_metric_history.
type MetricHistoryRow struct {
	Provider   string  `json:"provider"`
	AccountID  string  `json:"account_id"`
	MetricKind string  `json:"metric_kind"`
	Value      any     `json:"value"`
	ObservedAt string  `json:"observed_at"`
	ExpiresAt  *string `json:"expires_at"`
	Status     string  `json:"status"`
}

// InsertMetricHistory appends a history sample.
func (d *DB) InsertMetricHistory(ctx context.Context, row MetricHistoryRow) error {
	value, err := encodeJSON(row.Value)
	if err != nil {
		return err
	}
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT OR REPLACE INTO account_metric_history
				(provider, account_id, metric_kind, metric_value_json, observed_at, expires_at, status)
			VALUES (?, ?, ?, ?, ?, ?, ?)`,
			row.Provider, row.AccountID, row.MetricKind, value, row.ObservedAt, row.ExpiresAt,
			orDefault(row.Status, "fresh"))
		return err
	})
}

// ListMetricHistory returns history samples for one metric kind.
func (d *DB) ListMetricHistory(ctx context.Context, provider, accountID, metricKind, since string, limit int) ([]MetricHistoryRow, error) {
	limit = clamp(limit, 1, 2001)
	query := `SELECT provider, account_id, metric_kind, metric_value_json, observed_at, expires_at, status
	            FROM account_metric_history WHERE provider=? AND account_id=? AND metric_kind=?`
	args := []any{provider, accountID, metricKind}
	if since != "" {
		query += " AND observed_at >= ?"
		args = append(args, since)
	}
	query += " ORDER BY observed_at ASC LIMIT ?"
	args = append(args, limit)
	rows, err := d.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []MetricHistoryRow
	for rows.Next() {
		var row MetricHistoryRow
		var valueJSON string
		if err := rows.Scan(&row.Provider, &row.AccountID, &row.MetricKind, &valueJSON,
			&row.ObservedAt, &row.ExpiresAt, &row.Status); err != nil {
			return nil, err
		}
		row.Value = decodeAny(valueJSON)
		out = append(out, row)
	}
	return out, rows.Err()
}

// PruneMetricHistory deletes samples older than the retention cutoff.
func (d *DB) PruneMetricHistory(ctx context.Context, before string) (int, error) {
	deleted := 0
	err := d.Write(ctx, func(tx *sql.Tx) error {
		result, err := tx.ExecContext(ctx, "DELETE FROM account_metric_history WHERE observed_at < ?", before)
		if err != nil {
			return err
		}
		affected, err := result.RowsAffected()
		if err == nil {
			deleted = int(affected)
		}
		return nil
	})
	return deleted, err
}

// MetricRefreshOperation is one row of metric_refresh_operations.
type MetricRefreshOperation struct {
	OperationID string  `json:"operation_id"`
	Status      string  `json:"status"`
	Result      any     `json:"result"`
	ErrorCode   *string `json:"error_code"`
	CreatedAt   string  `json:"created_at"`
	FinishedAt  *string `json:"finished_at"`
}

// PutMetricRefreshOperation upserts a refresh operation row.
func (d *DB) PutMetricRefreshOperation(ctx context.Context, operation MetricRefreshOperation) error {
	result, err := encodeJSON(operation.Result)
	if err != nil {
		return err
	}
	if operation.CreatedAt == "" {
		operation.CreatedAt = NowISO()
	}
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT INTO metric_refresh_operations (operation_id, status, result_json, error_code, created_at, finished_at)
			VALUES (?, ?, ?, ?, ?, ?)
			ON CONFLICT(operation_id) DO UPDATE SET
				status=excluded.status, result_json=excluded.result_json,
				error_code=excluded.error_code, finished_at=excluded.finished_at`,
			operation.OperationID, operation.Status, result, operation.ErrorCode,
			operation.CreatedAt, operation.FinishedAt)
		return err
	})
}

// GetMetricRefreshOperation loads one operation row.
func (d *DB) GetMetricRefreshOperation(ctx context.Context, operationID string) (MetricRefreshOperation, error) {
	var operation MetricRefreshOperation
	var resultJSON string
	err := d.QueryRowContext(ctx, `
		SELECT operation_id, status, result_json, error_code, created_at, finished_at
		  FROM metric_refresh_operations WHERE operation_id=?`, operationID).
		Scan(&operation.OperationID, &operation.Status, &resultJSON, &operation.ErrorCode,
			&operation.CreatedAt, &operation.FinishedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return MetricRefreshOperation{}, ErrNotFound
	}
	if err != nil {
		return MetricRefreshOperation{}, err
	}
	operation.Result = decodeAny(resultJSON)
	return operation, nil
}

// OAuthFlow is one row of oauth_flows.
type OAuthFlow struct {
	StateHash string  `json:"-"`
	Provider  string  `json:"provider"`
	Label     *string `json:"label"`
	CreatedAt string  `json:"created_at"`
	ExpiresAt string  `json:"expires_at"`
	Status    string  `json:"status"`
	AccountID *string `json:"account_id"`
}

// PutOAuthFlow upserts a login flow row.
func (d *DB) PutOAuthFlow(ctx context.Context, flow OAuthFlow) error {
	if flow.CreatedAt == "" {
		flow.CreatedAt = NowISO()
	}
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT INTO oauth_flows (state_hash, provider, label, created_at, expires_at, status, account_id)
			VALUES (?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(state_hash) DO UPDATE SET
				status=excluded.status, account_id=excluded.account_id, label=excluded.label`,
			flow.StateHash, flow.Provider, flow.Label, flow.CreatedAt, flow.ExpiresAt,
			orDefault(flow.Status, "pending"), flow.AccountID)
		return err
	})
}

// GetOAuthFlow loads one login flow by state hash.
func (d *DB) GetOAuthFlow(ctx context.Context, stateHash string) (OAuthFlow, error) {
	var flow OAuthFlow
	err := d.QueryRowContext(ctx, `
		SELECT state_hash, provider, label, created_at, expires_at, status, account_id
		  FROM oauth_flows WHERE state_hash=?`, stateHash).
		Scan(&flow.StateHash, &flow.Provider, &flow.Label, &flow.CreatedAt, &flow.ExpiresAt,
			&flow.Status, &flow.AccountID)
	if errors.Is(err, sql.ErrNoRows) {
		return OAuthFlow{}, ErrNotFound
	}
	if err != nil {
		return OAuthFlow{}, err
	}
	return flow, nil
}

// PruneOAuthFlows deletes expired login flows.
func (d *DB) PruneOAuthFlows(ctx context.Context, before string) error {
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, "DELETE FROM oauth_flows WHERE expires_at < ?", before)
		return err
	})
}

// CompactHistoryPayloads drops per-package detail from stored metric history.
//
// “packages“ accounted for ~97% of the “points“ payload bytes while the
// history trend only reads scalar totals. Snapshot rows keep the full payload
// (the detail page renders packages); only history rows are trimmed.
func (d *DB) CompactHistoryPayloads(ctx context.Context) (int, error) {
	changed := 0
	err := d.Write(ctx, func(tx *sql.Tx) error {
		result, err := tx.ExecContext(ctx, `
			UPDATE account_metric_history
			   SET metric_value_json = json_remove(metric_value_json, '$.packages')
			 WHERE CASE WHEN json_valid(metric_value_json)
			            THEN json_type(metric_value_json, '$.packages') END = 'array'`)
		if err != nil {
			return err
		}
		affected, err := result.RowsAffected()
		if err == nil {
			changed = int(affected)
		}
		return nil
	})
	return changed, err
}

// LostAndFoundCount reports whether the SQLite recovery scratch table exists.
func (d *DB) LostAndFoundCount(ctx context.Context) (int, error) {
	var count int
	err := d.QueryRowContext(ctx,
		"SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='lost_and_found'").Scan(&count)
	return count, err
}

func orDefaultSlice(values []string) []string {
	if values == nil {
		return []string{}
	}
	return values
}

func orDefaultMap(values map[string]any) map[string]any {
	if values == nil {
		return map[string]any{}
	}
	return values
}
