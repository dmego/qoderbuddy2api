// Package metrics collects the account metric snapshots the console renders:
// per-purpose token expiry, the domestic check-in state, and the credit
// balances of both kept providers.
//
// Storage split (load-bearing): account_metric_snapshots keeps the FULL payload
// including the `packages` array, because the credits detail page renders one
// row per package. account_metric_history stores the same payload with
// `packages` removed — that array is ~97% of the bytes of a points payload
// while the trend chart reads only the scalar totals. Keeping the array in both
// places grew the history table to 133 MiB for 91k rows.
package metrics

import (
	"context"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"

	"github.com/dmego/qoderbuddy2api/gobackend/internal/config"
	"github.com/dmego/qoderbuddy2api/gobackend/internal/models"
	"github.com/dmego/qoderbuddy2api/gobackend/internal/store"
	"github.com/dmego/qoderbuddy2api/gobackend/internal/vault"
)

// Metric kinds persisted in account_metric_snapshots / account_metric_history.
const (
	KindCheckin      = "checkin"
	KindPoints       = "points"
	KindTokenChat    = "token:chat"
	KindTokenCheckin = "token:checkin"
)

// Row statuses. Token snapshots reuse valid/expired as their row status so the
// console can read a credential's lifetime without decoding the value.
const (
	statusFresh       = "fresh"
	statusStale       = "stale"
	statusUnknown     = "unknown"
	statusUnavailable = "unavailable"
	statusSkipped     = "skipped"
	statusValid       = "valid"
	statusExpired     = "expired"

	backoffError = "backoff"
)

// metric_refresh_operations status values.
const (
	operationRunning   = "running"
	operationSucceeded = "succeeded"
	operationFailed    = "failed"
	operationCancelled = "cancelled"

	errorCodeFailed    = "metrics_refresh_failed"
	errorCodeCancelled = "refresh_cancelled"
)

// Credential is the resolved auth material for one account read.
type Credential struct {
	Mode        string
	AccessToken string
	Cookie      string
}

// ProviderClients performs the provider-specific upstream reads.
type ProviderClients interface {
	FetchCredits(ctx context.Context, provider, accountID string, cred Credential) (map[string]any, error)
	FetchCheckinStatus(ctx context.Context, provider, accountID string, cred Credential) (map[string]any, error)
}

// AccountRef identifies one account to sample.
type AccountRef struct {
	Provider  string
	AccountID string
}

// AccountLister yields the accounts the collector samples for a provider.
type AccountLister interface {
	EligibleAccounts(provider string) []AccountRef
	Rebuild(ctx context.Context) error
}

// CollectorOptions configures the collector.
type CollectorOptions struct {
	DB       *store.DB
	Vault    *vault.Vault
	Settings config.Settings
	Accounts AccountLister
	Clients  ProviderClients
}

// Collector refreshes metric snapshots for the kept providers.
type Collector struct {
	opts CollectorOptions

	// roundMu serializes whole rounds so an admin-triggered refresh and the
	// scheduled round cannot hit the same upstream twice at once.
	roundMu sync.Mutex
	// mu guards backoff, which outlives a single round and is also touched by
	// per-account refreshes issued from the check-in pipeline.
	mu      sync.Mutex
	backoff map[metricKey]backoffEntry
	// now is injectable so the backoff policy is testable without sleeping.
	now func() time.Time
	// collecting reports whether a round currently holds the single-flight
	// slot. It is atomic so the status view never blocks on a running round.
	collecting atomic.Bool
}

// RefreshInProgress reports whether a collection round is running.
func (c *Collector) RefreshInProgress() bool { return c.collecting.Load() }

// metricKey identifies one stored metric row.
type metricKey struct {
	provider string
	account  string
	kind     string
}

func (k metricKey) String() string { return k.provider + ":" + k.account + ":" + k.kind }

// backoffEntry tracks one metric's consecutive failures.
type backoffEntry struct {
	attempts int
	retryAt  time.Time
}

// collectState carries one round's tally and the prior snapshots it compares
// against.
type collectState struct {
	previous map[metricKey]store.MetricSnapshot
	counts   map[string]int
	seen     map[metricKey]bool
}

// NewCollector builds the collector.
func NewCollector(opts CollectorOptions) *Collector {
	return &Collector{opts: opts, backoff: map[metricKey]backoffEntry{}, now: time.Now}
}

// RefreshAll collects every eligible account and records a
// metric_refresh_operations row so the console can poll the outcome.
func (c *Collector) RefreshAll(ctx context.Context, operationID string) {
	if err := c.opts.DB.PutMetricRefreshOperation(ctx, store.MetricRefreshOperation{
		OperationID: operationID,
		Status:      operationRunning,
		Result:      map[string]any{},
	}); err != nil {
		slog.Warn("metrics refresh operation could not be recorded", "operation_id", operationID, "error", err)
		return
	}
	counts, err := c.Collect(ctx)
	operation := store.MetricRefreshOperation{
		OperationID: operationID,
		Status:      operationSucceeded,
		Result:      counts,
	}
	switch {
	case err == nil:
	case ctx.Err() != nil:
		// A shutdown or an aborted round is not an upstream failure; saying so
		// keeps the console from reporting a problem that never happened.
		operation.Status = operationCancelled
		operation.ErrorCode = stringPtr(errorCodeCancelled)
	default:
		operation.Status = operationFailed
		operation.ErrorCode = stringPtr(errorCodeFailed)
		slog.Warn("metrics refresh round failed", "operation_id", operationID, "error", err)
	}
	finished := store.NowISO()
	operation.FinishedAt = &finished
	if err := c.opts.DB.PutMetricRefreshOperation(ctx, operation); err != nil {
		slog.Warn("metrics refresh result could not be recorded", "operation_id", operationID, "error", err)
	}
}

// RefreshOne collects one account. The returned payload is the provider credit
// aggregate as stored on the snapshot row (packages included), which is what
// the check-in pipeline reads back after a claim; it is nil for a provider with
// no credit read.
func (c *Collector) RefreshOne(ctx context.Context, provider, accountID string) (map[string]any, error) {
	previous, err := c.previousRows(ctx, provider, accountID)
	if err != nil {
		return nil, err
	}
	state := &collectState{
		previous: previous,
		counts:   emptyCounts(),
		seen:     map[metricKey]bool{},
	}
	result, err := c.collectAccount(ctx, state, provider, accountID)
	if err != nil {
		return nil, err
	}
	return result.Points, result.PointsErr
}

// Collect runs one round over every eligible account of both providers and
// prunes history that fell out of the retention window. The returned map is the
// per-status tally the refresh operation reports.
func (c *Collector) Collect(ctx context.Context) (map[string]int, error) {
	c.roundMu.Lock()
	defer c.roundMu.Unlock()
	c.collecting.Store(true)
	defer c.collecting.Store(false)
	if err := c.opts.Accounts.Rebuild(ctx); err != nil {
		return emptyCounts(), err
	}
	previous, err := c.previousRows(ctx, "", "")
	if err != nil {
		return emptyCounts(), err
	}
	state := &collectState{
		previous: previous,
		counts:   emptyCounts(),
		seen:     map[metricKey]bool{},
	}
	for _, provider := range models.KnownProviders {
		for _, ref := range c.opts.Accounts.EligibleAccounts(provider) {
			if err := ctx.Err(); err != nil {
				return state.counts, err
			}
			// A metric failure is recorded on its own row already, so the
			// remaining accounts still get their snapshot this round.
			if _, err := c.collectAccount(ctx, state, ref.Provider, ref.AccountID); err != nil {
				return state.counts, err
			}
		}
	}
	c.countUnseen(state)
	if err := c.prune(ctx); err != nil {
		return state.counts, err
	}
	return state.counts, nil
}

// previousRows loads the snapshots a round compares against, optionally scoped
// to one account.
func (c *Collector) previousRows(ctx context.Context, provider, accountID string) (map[metricKey]store.MetricSnapshot, error) {
	rows, err := c.opts.DB.ListMetricSnapshots(ctx, provider, accountID)
	if err != nil {
		return nil, err
	}
	out := make(map[metricKey]store.MetricSnapshot, len(rows))
	for _, row := range rows {
		out[metricKey{row.Provider, row.AccountID, row.MetricKind}] = row
	}
	return out, nil
}

func emptyCounts() map[string]int {
	return map[string]int{
		statusFresh:       0,
		statusStale:       0,
		statusUnknown:     0,
		statusUnavailable: 0,
		statusSkipped:     0,
	}
}

func stringPtr(value string) *string { return &value }
