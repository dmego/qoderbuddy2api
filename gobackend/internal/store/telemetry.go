package store

import (
	"context"
	"database/sql"
	"math"
	"sort"
	"strings"
	"sync"
	"time"
)

// RequestEvent is one row of request_events.
type RequestEvent struct {
	EventID         string  `json:"event_id"`
	RequestID       string  `json:"request_id"`
	Provider        string  `json:"provider"`
	AccountID       *string `json:"account_id"`
	ModelID         string  `json:"model_id"`
	Protocol        string  `json:"protocol"`
	Status          string  `json:"status"`
	HTTPStatus      *int    `json:"http_status"`
	InputTokens     *int    `json:"input_tokens"`
	OutputTokens    *int    `json:"output_tokens"`
	LatencyMS       *int    `json:"latency_ms"`
	FirstTokenMS    *int    `json:"first_token_ms"`
	StreamCommitted bool    `json:"stream_committed"`
	StartedAt       string  `json:"started_at"`
	FinishedAt      *string `json:"finished_at"`
	ErrorCode       *string `json:"error_code"`
	RedactedError   *string `json:"redacted_error"`
	ReasoningEffort *string `json:"reasoning_effort"`
}

// EventWriter batches request-event inserts.
//
// The Python control plane issued one write transaction (plus a WAL fsync) per
// proxied request. Events are pure telemetry: a bounded delay before they land
// costs nothing observable, while coalescing hundreds of rows into one
// transaction removes nearly all of the write traffic.
type EventWriter struct {
	db         *DB
	flushEvery time.Duration
	maxBatch   int

	mu      sync.Mutex
	pending []RequestEvent
	timer   *time.Timer
	stopped bool

	written int64
	dropped int64
	lastErr error
}

// NewEventWriter starts a batching writer. A zero flushEvery or maxBatch falls
// back to a sane default.
func NewEventWriter(db *DB, flushEvery time.Duration, maxBatch int) *EventWriter {
	if flushEvery <= 0 {
		flushEvery = 250 * time.Millisecond
	}
	if maxBatch <= 0 {
		maxBatch = 200
	}
	return &EventWriter{db: db, flushEvery: flushEvery, maxBatch: maxBatch}
}

// Enqueue adds an event to the pending batch. It never blocks the request path.
func (w *EventWriter) Enqueue(event RequestEvent) {
	if event.StartedAt == "" {
		event.StartedAt = NowISO()
	}
	if event.FinishedAt == nil {
		finished := event.StartedAt
		event.FinishedAt = &finished
	}
	w.mu.Lock()
	if w.stopped {
		w.dropped++
		w.mu.Unlock()
		return
	}
	w.pending = append(w.pending, event)
	shouldFlush := len(w.pending) >= w.maxBatch
	if !shouldFlush && w.timer == nil {
		w.timer = time.AfterFunc(w.flushEvery, func() { _ = w.Flush(context.Background()) })
	}
	w.mu.Unlock()
	if shouldFlush {
		_ = w.Flush(context.Background())
	}
}

// Flush writes the pending batch. It is safe to call concurrently and from the
// shutdown path.
func (w *EventWriter) Flush(ctx context.Context) error {
	w.mu.Lock()
	batch := w.pending
	w.pending = nil
	if w.timer != nil {
		w.timer.Stop()
		w.timer = nil
	}
	w.mu.Unlock()
	if len(batch) == 0 {
		return nil
	}
	err := w.db.Write(ctx, func(tx *sql.Tx) error {
		statement, err := tx.PrepareContext(ctx, `
			INSERT OR IGNORE INTO request_events
				(event_id, request_id, provider, account_id, model_id, protocol, status, http_status,
				 input_tokens, output_tokens, latency_ms, stream_committed, started_at, finished_at,
				 error_code, redacted_error, reasoning_effort, first_token_ms)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
		if err != nil {
			return err
		}
		defer statement.Close()
		for _, event := range batch {
			if _, err := statement.ExecContext(ctx,
				event.EventID, event.RequestID, event.Provider, event.AccountID, event.ModelID,
				event.Protocol, event.Status, event.HTTPStatus, event.InputTokens, event.OutputTokens,
				event.LatencyMS, boolInt(event.StreamCommitted), event.StartedAt, event.FinishedAt,
				event.ErrorCode, event.RedactedError, event.ReasoningEffort, event.FirstTokenMS); err != nil {
				return err
			}
		}
		return nil
	})
	w.mu.Lock()
	w.written += int64(len(batch))
	if err != nil {
		w.lastErr = err
		// Losing telemetry must never break proxying, so a failed batch is
		// counted rather than retried into a growing backlog.
		w.dropped += int64(len(batch))
	}
	w.mu.Unlock()
	return err
}

// Stats reports writer health for the admin service view.
func (w *EventWriter) Stats() (written, dropped int64, lastErr error, pending int) {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.written, w.dropped, w.lastErr, len(w.pending)
}

// Close flushes and stops the writer.
func (w *EventWriter) Close(ctx context.Context) error {
	w.mu.Lock()
	w.stopped = true
	w.mu.Unlock()
	return w.Flush(ctx)
}

// UsageFilter narrows a request-event query.
type UsageFilter struct {
	Provider      string
	AccountID     string
	ModelID       string
	Status        string
	StartedAfter  string
	StartedBefore string
}

func (f UsageFilter) clause(timeColumn string) (string, []any) {
	clauses := []string{}
	args := []any{}
	for _, pair := range [][2]string{
		{"provider", f.Provider},
		{"account_id", f.AccountID},
		{"model_id", f.ModelID},
		{"status", f.Status},
	} {
		if pair[1] != "" {
			clauses = append(clauses, pair[0]+"=?")
			args = append(args, pair[1])
		}
	}
	if f.StartedAfter != "" {
		clauses = append(clauses, timeColumn+">=?")
		args = append(args, f.StartedAfter)
	}
	if f.StartedBefore != "" {
		clauses = append(clauses, timeColumn+"<?")
		args = append(args, f.StartedBefore)
	}
	if len(clauses) == 0 {
		return "1=1", args
	}
	return strings.Join(clauses, " AND "), args
}

// ListRequestEvents returns events newest-first.
func (d *DB) ListRequestEvents(ctx context.Context, filter UsageFilter, limit, offset int) ([]RequestEvent, error) {
	limit = clamp(limit, 1, 501)
	if offset < 0 {
		offset = 0
	}
	where, args := filter.clause("started_at")
	args = append(args, limit, offset)
	rows, err := d.QueryContext(ctx, `
		SELECT event_id, request_id, provider, account_id, model_id, protocol, status, http_status,
		       input_tokens, output_tokens, latency_ms, stream_committed, started_at, finished_at,
		       error_code, redacted_error, reasoning_effort, first_token_ms
		  FROM request_events WHERE `+where+`
		 ORDER BY started_at DESC, event_id DESC LIMIT ? OFFSET ?`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanEvents(rows)
}

// GetRequestEvent loads one event.
func (d *DB) GetRequestEvent(ctx context.Context, eventID string) (RequestEvent, error) {
	rows, err := d.QueryContext(ctx, `
		SELECT event_id, request_id, provider, account_id, model_id, protocol, status, http_status,
		       input_tokens, output_tokens, latency_ms, stream_committed, started_at, finished_at,
		       error_code, redacted_error, reasoning_effort, first_token_ms
		  FROM request_events WHERE event_id=?`, eventID)
	if err != nil {
		return RequestEvent{}, err
	}
	defer rows.Close()
	events, err := scanEvents(rows)
	if err != nil {
		return RequestEvent{}, err
	}
	if len(events) == 0 {
		return RequestEvent{}, ErrNotFound
	}
	return events[0], nil
}

func scanEvents(rows *sql.Rows) ([]RequestEvent, error) {
	var out []RequestEvent
	for rows.Next() {
		var event RequestEvent
		var committed int
		if err := rows.Scan(&event.EventID, &event.RequestID, &event.Provider, &event.AccountID,
			&event.ModelID, &event.Protocol, &event.Status, &event.HTTPStatus, &event.InputTokens,
			&event.OutputTokens, &event.LatencyMS, &committed, &event.StartedAt, &event.FinishedAt,
			&event.ErrorCode, &event.RedactedError, &event.ReasoningEffort, &event.FirstTokenMS); err != nil {
			return nil, err
		}
		event.StreamCommitted = committed != 0
		out = append(out, event)
	}
	return out, rows.Err()
}

// UsageSummary is the aggregate shown on the usage page.
type UsageSummary struct {
	RequestCount      int      `json:"request_count"`
	InputTokens       int      `json:"input_tokens"`
	OutputTokens      int      `json:"output_tokens"`
	SuccessCount      int      `json:"success_count"`
	ErrorCount        int      `json:"error_count"`
	TokenEventCount   int      `json:"token_event_count"`
	MissingTokenCount int      `json:"missing_token_count"`
	LatencyAvgMS      *float64 `json:"latency_avg_ms"`
	LatencyP95MS      *int     `json:"latency_p95_ms"`
	TTFTAvgMS         *float64 `json:"ttft_avg_ms"`
	TTFTP95MS         *int     `json:"ttft_p95_ms"`
}

// SummarizeUsage computes the usage-page aggregate for a filter.
func (d *DB) SummarizeUsage(ctx context.Context, filter UsageFilter) (UsageSummary, error) {
	where, args := filter.clause("started_at")
	var summary UsageSummary
	err := d.QueryRowContext(ctx, `
		SELECT COUNT(*), COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),
		       COALESCE(SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END), 0),
		       COALESCE(SUM(CASE WHEN status!='succeeded' THEN 1 ELSE 0 END), 0),
		       COALESCE(SUM(CASE WHEN input_tokens IS NOT NULL OR output_tokens IS NOT NULL THEN 1 ELSE 0 END), 0),
		       COALESCE(SUM(CASE WHEN input_tokens IS NULL AND output_tokens IS NULL THEN 1 ELSE 0 END), 0)
		  FROM request_events WHERE `+where, args...).
		Scan(&summary.RequestCount, &summary.InputTokens, &summary.OutputTokens, &summary.SuccessCount,
			&summary.ErrorCount, &summary.TokenEventCount, &summary.MissingTokenCount)
	if err != nil {
		return UsageSummary{}, err
	}
	latencies, err := d.latencyValues(ctx, where, args, "latency_ms")
	if err != nil {
		return UsageSummary{}, err
	}
	if len(latencies) > 0 {
		summary.LatencyAvgMS = floatPtr(float64(sum(latencies)) / float64(len(latencies)))
		summary.LatencyP95MS = intPtr(percentileSummary(latencies, 0.95))
	}
	ttfts, err := d.latencyValues(ctx, where, args, "first_token_ms")
	if err != nil {
		return UsageSummary{}, err
	}
	if len(ttfts) > 0 {
		summary.TTFTAvgMS = floatPtr(float64(sum(ttfts)) / float64(len(ttfts)))
		summary.TTFTP95MS = intPtr(percentileSummary(ttfts, 0.95))
	}
	return summary, nil
}

func (d *DB) latencyValues(ctx context.Context, where string, args []any, column string) ([]int, error) {
	rows, err := d.QueryContext(ctx,
		"SELECT "+column+" FROM request_events WHERE "+where+" AND "+column+" IS NOT NULL ORDER BY "+column,
		args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var values []int
	for rows.Next() {
		var value int
		if err := rows.Scan(&value); err != nil {
			return nil, err
		}
		values = append(values, value)
	}
	return values, rows.Err()
}

// Rollup is one row of usage_rollups.
type Rollup struct {
	BucketStart       string  `json:"bucket_start"`
	BucketKind        string  `json:"bucket_kind"`
	Provider          string  `json:"provider"`
	AccountID         *string `json:"account_id"`
	ModelID           string  `json:"model_id"`
	RequestCount      int     `json:"request_count"`
	SuccessCount      int     `json:"success_count"`
	ErrorCount        int     `json:"error_count"`
	InputTokens       int     `json:"input_tokens"`
	OutputTokens      int     `json:"output_tokens"`
	TokenEventCount   int     `json:"token_event_count"`
	MissingTokenCount int     `json:"missing_token_count"`
	LatencyP50MS      *int    `json:"latency_p50_ms"`
	LatencyP95MS      *int    `json:"latency_p95_ms"`
	LatencyAvgMS      *int    `json:"latency_avg_ms"`
	TTFTAvgMS         *int    `json:"ttft_avg_ms"`
	TTFTP95MS         *int    `json:"ttft_p95_ms"`
	UpdatedAt         string  `json:"updated_at"`
}

// ListRollups returns stored rollup buckets newest-first.
func (d *DB) ListRollups(ctx context.Context, filter UsageFilter, bucketKind string, limit, offset int) ([]Rollup, error) {
	limit = clamp(limit, 1, 501)
	if offset < 0 {
		offset = 0
	}
	where, args := filter.clause("bucket_start")
	if bucketKind != "" {
		where += " AND bucket_kind=?"
		args = append(args, bucketKind)
	}
	args = append(args, limit, offset)
	rows, err := d.QueryContext(ctx, `
		SELECT bucket_start, bucket_kind, provider, account_id, model_id, request_count, success_count,
		       error_count, input_tokens, output_tokens, token_event_count, missing_token_count,
		       latency_p50_ms, latency_p95_ms, latency_avg_ms, ttft_avg_ms, ttft_p95_ms, updated_at
		  FROM usage_rollups WHERE `+where+`
		 ORDER BY bucket_start DESC, provider, account_id, model_id LIMIT ? OFFSET ?`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Rollup
	for rows.Next() {
		var rollup Rollup
		if err := rows.Scan(&rollup.BucketStart, &rollup.BucketKind, &rollup.Provider, &rollup.AccountID,
			&rollup.ModelID, &rollup.RequestCount, &rollup.SuccessCount, &rollup.ErrorCount,
			&rollup.InputTokens, &rollup.OutputTokens, &rollup.TokenEventCount, &rollup.MissingTokenCount,
			&rollup.LatencyP50MS, &rollup.LatencyP95MS, &rollup.LatencyAvgMS, &rollup.TTFTAvgMS,
			&rollup.TTFTP95MS, &rollup.UpdatedAt); err != nil {
			return nil, err
		}
		out = append(out, rollup)
	}
	return out, rows.Err()
}

// UpsertRollup writes one aggregated bucket.
func upsertRollup(ctx context.Context, tx *sql.Tx, rollup Rollup) error {
	_, err := tx.ExecContext(ctx, `
		INSERT INTO usage_rollups
			(bucket_start, bucket_kind, provider, account_id, model_id, request_count, success_count,
			 error_count, input_tokens, output_tokens, token_event_count, missing_token_count,
			 latency_p50_ms, latency_p95_ms, latency_avg_ms, ttft_avg_ms, ttft_p95_ms, updated_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(bucket_start, bucket_kind, provider, account_id, model_id) DO UPDATE SET
			request_count=excluded.request_count, success_count=excluded.success_count,
			error_count=excluded.error_count, input_tokens=excluded.input_tokens,
			output_tokens=excluded.output_tokens, token_event_count=excluded.token_event_count,
			missing_token_count=excluded.missing_token_count, latency_p50_ms=excluded.latency_p50_ms,
			latency_p95_ms=excluded.latency_p95_ms, latency_avg_ms=excluded.latency_avg_ms,
			ttft_avg_ms=excluded.ttft_avg_ms, ttft_p95_ms=excluded.ttft_p95_ms,
			updated_at=excluded.updated_at`,
		rollup.BucketStart, rollup.BucketKind, rollup.Provider, rollup.AccountID, rollup.ModelID,
		rollup.RequestCount, rollup.SuccessCount, rollup.ErrorCount, rollup.InputTokens,
		rollup.OutputTokens, rollup.TokenEventCount, rollup.MissingTokenCount, rollup.LatencyP50MS,
		rollup.LatencyP95MS, rollup.LatencyAvgMS, rollup.TTFTAvgMS, rollup.TTFTP95MS, NowISO())
	return err
}

// PruneRequestEvents deletes detail rows older than the cutoff.
func (d *DB) PruneRequestEvents(ctx context.Context, before string) (int, error) {
	deleted := 0
	err := d.Write(ctx, func(tx *sql.Tx) error {
		result, err := tx.ExecContext(ctx, "DELETE FROM request_events WHERE started_at < ?", before)
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

// eventForRollup is the projection the rollup aggregator reads.
type eventForRollup struct {
	StartedAt    string
	Provider     string
	AccountID    string
	ModelID      string
	Status       string
	InputTokens  *int
	OutputTokens *int
	LatencyMS    *int
	FirstTokenMS *int
}

// EventsBetween loads the columns the rollup needs for a time range.
func (d *DB) EventsBetween(ctx context.Context, startedAt, endedAt string) ([]eventForRollup, error) {
	rows, err := d.QueryContext(ctx, `
		SELECT started_at, provider, COALESCE(account_id, ''), model_id, status,
		       input_tokens, output_tokens, latency_ms, first_token_ms
		  FROM request_events WHERE started_at >= ? AND started_at < ?`, startedAt, endedAt)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []eventForRollup
	for rows.Next() {
		var event eventForRollup
		if err := rows.Scan(&event.StartedAt, &event.Provider, &event.AccountID, &event.ModelID,
			&event.Status, &event.InputTokens, &event.OutputTokens, &event.LatencyMS,
			&event.FirstTokenMS); err != nil {
			return nil, err
		}
		out = append(out, event)
	}
	return out, rows.Err()
}

// RollupWindow is one aggregation window.
type RollupWindow struct {
	Kind  string
	Start time.Time
	End   time.Time
}

// RollupWindows returns the nested minute/day/month windows for an instant.
func RollupWindows(now time.Time) []RollupWindow {
	utc := now.UTC()
	minute := time.Date(utc.Year(), utc.Month(), utc.Day(), utc.Hour(), utc.Minute(), 0, 0, time.UTC)
	day := time.Date(utc.Year(), utc.Month(), utc.Day(), 0, 0, 0, 0, time.UTC)
	month := time.Date(utc.Year(), utc.Month(), 1, 0, 0, 0, 0, time.UTC)
	return []RollupWindow{
		{Kind: "minute", Start: minute, End: minute.Add(time.Minute)},
		{Kind: "day", Start: day, End: day.AddDate(0, 0, 1)},
		{Kind: "month", Start: month, End: month.AddDate(0, 1, 0)},
	}
}

// RollupOnce recomputes the nested windows and prunes expired detail.
//
// The windows nest (minute ⊂ day ⊂ month), so one read of the widest window
// carries every bucket — reading per window re-decoded the same rows up to
// three times per cycle for identical results. All buckets are written in one
// transaction so a cycle costs a single fsync.
func (d *DB) RollupOnce(ctx context.Context, now time.Time, retentionDays int) (groups, deleted int, err error) {
	windows := RollupWindows(now)
	widest := windows[len(windows)-1]
	events, err := d.EventsBetween(ctx, FormatISO(widest.Start), FormatISO(widest.End))
	if err != nil {
		return 0, 0, err
	}
	var pending []Rollup
	for _, window := range windows {
		within := eventsWithin(events, window)
		pending = append(pending, aggregate(within, window)...)
	}
	err = d.Write(ctx, func(tx *sql.Tx) error {
		for _, rollup := range pending {
			if err := upsertRollup(ctx, tx, rollup); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return 0, 0, err
	}
	cutoff := now.UTC().AddDate(0, 0, -max(1, retentionDays))
	deleted, err = d.PruneRequestEvents(ctx, FormatISO(cutoff))
	if err != nil {
		return len(pending), 0, err
	}
	return len(pending), deleted, nil
}

// eventsWithin narrows a wider read to one window. started_at is a fixed-width
// UTC ISO string, so the same lexicographic comparison SQLite applies is exact.
func eventsWithin(events []eventForRollup, window RollupWindow) []eventForRollup {
	start := FormatISO(window.Start)
	end := FormatISO(window.End)
	out := make([]eventForRollup, 0, len(events))
	for _, event := range events {
		if event.StartedAt >= start && event.StartedAt < end {
			out = append(out, event)
		}
	}
	return out
}

func aggregate(events []eventForRollup, window RollupWindow) []Rollup {
	type key struct{ provider, accountID, modelID string }
	grouped := map[key][]eventForRollup{}
	order := []key{}
	for _, event := range events {
		groupKey := key{provider: orDefault(event.Provider, "unknown"), accountID: event.AccountID, modelID: orDefault(event.ModelID, "unknown")}
		if _, seen := grouped[groupKey]; !seen {
			order = append(order, groupKey)
		}
		grouped[groupKey] = append(grouped[groupKey], event)
	}
	out := make([]Rollup, 0, len(order))
	for _, groupKey := range order {
		rows := grouped[groupKey]
		rollup := Rollup{
			BucketStart:  FormatISO(window.Start),
			BucketKind:   window.Kind,
			Provider:     groupKey.provider,
			ModelID:      groupKey.modelID,
			RequestCount: len(rows),
		}
		if groupKey.accountID != "" {
			account := groupKey.accountID
			rollup.AccountID = &account
		}
		var latencies, ttfts []int
		for _, row := range rows {
			if row.Status == "succeeded" {
				rollup.SuccessCount++
			} else {
				rollup.ErrorCount++
			}
			if row.InputTokens != nil || row.OutputTokens != nil {
				rollup.TokenEventCount++
				if row.InputTokens != nil {
					rollup.InputTokens += *row.InputTokens
				}
				if row.OutputTokens != nil {
					rollup.OutputTokens += *row.OutputTokens
				}
			} else {
				rollup.MissingTokenCount++
			}
			if row.LatencyMS != nil {
				latencies = append(latencies, *row.LatencyMS)
			}
			if row.FirstTokenMS != nil {
				ttfts = append(ttfts, *row.FirstTokenMS)
			}
		}
		if len(latencies) > 0 {
			sort.Ints(latencies)
			rollup.LatencyP50MS = intPtr(percentileRollup(latencies, 0.50))
			rollup.LatencyP95MS = intPtr(percentileRollup(latencies, 0.95))
			average := sum(latencies) / len(latencies)
			rollup.LatencyAvgMS = &average
		}
		if len(ttfts) > 0 {
			sort.Ints(ttfts)
			average := sum(ttfts) / len(ttfts)
			rollup.TTFTAvgMS = &average
			rollup.TTFTP95MS = intPtr(percentileRollup(ttfts, 0.95))
		}
		out = append(out, rollup)
	}
	return out
}

// percentileSummary is the read-path percentile used by /usage/summary.
//
// The Python backend had TWO different percentile formulas and they genuinely
// disagree (for n=12 at p95 they return different elements). Unifying them
// would silently change numbers the console already showed, so both are ported
// verbatim: summary uses ceil(n*f)-1 over an unsorted input.
func percentileSummary(values []int, fraction float64) int {
	if len(values) == 0 {
		return 0
	}
	ordered := append([]int(nil), values...)
	sort.Ints(ordered)
	index := int(math.Ceil(float64(len(ordered))*fraction)) - 1
	if index < 0 {
		index = 0
	}
	if index >= len(ordered) {
		index = len(ordered) - 1
	}
	return ordered[index]
}

// percentileRollup is the aggregation-path percentile used by usage_rollups.
//
// It takes an already-sorted slice and uses round((n-1)*f) with Python's
// round-half-to-even, which differs from percentileSummary.
func percentileRollup(sorted []int, fraction float64) int {
	if len(sorted) == 0 {
		return 0
	}
	index := int(bankerRound(float64(len(sorted)-1) * fraction))
	if index < 0 {
		index = 0
	}
	if index >= len(sorted) {
		index = len(sorted) - 1
	}
	return sorted[index]
}

// bankerRound rounds half to even, matching Python's built-in round().
func bankerRound(value float64) float64 {
	floor := math.Floor(value)
	diff := value - floor
	switch {
	case diff > 0.5:
		return floor + 1
	case diff < 0.5:
		return floor
	default:
		if math.Mod(floor, 2) == 0 {
			return floor
		}
		return floor + 1
	}
}

func sum(values []int) int {
	total := 0
	for _, value := range values {
		total += value
	}
	return total
}

func clamp(value, low, high int) int {
	if value < low {
		return low
	}
	if value > high {
		return high
	}
	return value
}

func intPtr(value int) *int { return &value }

func floatPtr(value float64) *float64 { return &value }
