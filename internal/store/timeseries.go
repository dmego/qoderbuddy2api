package store

import (
	"context"
	"time"
)

// TimeseriesFromEvents aggregates request events into rollup buckets on the fly.
//
// The stored usage_rollups table has no status dimension, so a status-filtered
// timeseries cannot be served from it. This path exists because the console
// offers a status filter on the trend chart and silently showing unfiltered
// numbers would be wrong.
func (d *DB) TimeseriesFromEvents(ctx context.Context, filter UsageFilter, bucketKind string, limit int) ([]Rollup, error) {
	limit = clamp(limit, 1, 501)
	where, args := filter.clause("started_at")
	format := bucketFormat(bucketKind)
	rows, err := d.QueryContext(ctx, `
		SELECT strftime('`+format+`', started_at) AS bucket_start, provider,
		       COALESCE(account_id, '') AS account_id, model_id,
		       COUNT(*) AS request_count,
		       SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) AS success_count,
		       SUM(CASE WHEN status!='succeeded' THEN 1 ELSE 0 END) AS error_count,
		       COALESCE(SUM(input_tokens), 0) AS input_tokens,
		       COALESCE(SUM(output_tokens), 0) AS output_tokens,
		       SUM(CASE WHEN input_tokens IS NOT NULL OR output_tokens IS NOT NULL THEN 1 ELSE 0 END) AS token_event_count,
		       SUM(CASE WHEN input_tokens IS NULL AND output_tokens IS NULL THEN 1 ELSE 0 END) AS missing_token_count,
		       AVG(latency_ms) AS latency_avg_ms,
		       AVG(first_token_ms) AS ttft_avg_ms
		  FROM request_events WHERE `+where+`
		 GROUP BY bucket_start, provider, account_id, model_id
		 ORDER BY bucket_start DESC, provider, account_id, model_id LIMIT ?`, append(args, limit)...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Rollup
	for rows.Next() {
		var rollup Rollup
		var accountID string
		var latencyAvg, ttftAvg *float64
		if err := rows.Scan(&rollup.BucketStart, &rollup.Provider, &accountID, &rollup.ModelID,
			&rollup.RequestCount, &rollup.SuccessCount, &rollup.ErrorCount, &rollup.InputTokens,
			&rollup.OutputTokens, &rollup.TokenEventCount, &rollup.MissingTokenCount,
			&latencyAvg, &ttftAvg); err != nil {
			return nil, err
		}
		rollup.BucketKind = bucketKind
		rollup.AccountID = &accountID
		if latencyAvg != nil {
			value := int(*latencyAvg)
			rollup.LatencyAvgMS = &value
		}
		if ttftAvg != nil {
			value := int(*ttftAvg)
			rollup.TTFTAvgMS = &value
		}
		out = append(out, rollup)
	}
	return out, rows.Err()
}

// bucketFormat maps a bucket kind to the SQLite strftime pattern. The output
// keeps the "+00:00" offset so bucket_start sorts lexicographically alongside
// stored rows.
func bucketFormat(bucketKind string) string {
	switch bucketKind {
	case "day":
		return "%Y-%m-%dT00:00:00+00:00"
	case "month":
		return "%Y-%m-01T00:00:00+00:00"
	}
	return "%Y-%m-%dT%H:%M:00+00:00"
}

// ActiveAccounts lists the accounts eligible for chat, used by the collectors.
func (d *DB) ActiveAccounts(ctx context.Context, providers []string) ([]Account, error) {
	accounts, err := d.ListAccounts(ctx, providers)
	if err != nil {
		return nil, err
	}
	out := make([]Account, 0, len(accounts))
	for _, account := range accounts {
		if account.Enabled {
			out = append(out, account)
		}
	}
	return out, nil
}

// NowMinuteStart returns the current minute boundary in UTC.
func NowMinuteStart() time.Time {
	now := time.Now().UTC()
	return time.Date(now.Year(), now.Month(), now.Day(), now.Hour(), now.Minute(), 0, 0, time.UTC)
}
