package metrics

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/models"
	"github.com/dmego/qoderbuddy2api/internal/store"
)

// metricFailure reports one metric's upstream read failing. The collector
// records such a failure on the metric's own row and keeps going; only the
// credit metric's failure is returned, because the check-in pipeline consumes
// that value to compute the reward delta.
type metricFailure struct {
	key   metricKey
	cause string
}

func (f *metricFailure) Error() string { return f.key.String() + ": " + f.cause }

// isMetricFailure distinguishes a recorded metric failure from a storage error
// that must abort the round.
func isMetricFailure(err error) bool {
	var failure *metricFailure
	return errors.As(err, &failure)
}

// accountResult is one account's sampled credit payload.
type accountResult struct {
	// Points is the credit aggregate as stored, packages included. Nil when the
	// read failed or the provider publishes no balance.
	Points map[string]any
	// PointsErr is the recorded failure, if any.
	PointsErr error
}

// collectAccount samples one account: a token snapshot per enabled purpose, the
// check-in state for providers that have one, and the provider credit read.
// The returned error is fatal to the round only when storage itself failed.
func (c *Collector) collectAccount(ctx context.Context, state *collectState, provider, accountID string) (accountResult, error) {
	purposes, err := c.enabledPurposes(ctx, provider, accountID)
	if err != nil {
		return accountResult{}, err
	}
	now := c.clock()
	for _, purpose := range purposes {
		status := tokenStatus(purpose.ExpiresAt, now)
		value := map[string]any{"status": status, "expires_at": purpose.ExpiresAt}
		if err := c.write(ctx, state, metricKey{provider, accountID, "token:" + purpose.Purpose}, value, status, nil, nil); err != nil {
			return accountResult{}, err
		}
	}
	credential, credentialErr := c.resolveCredential(ctx, provider, accountID, credentialPurposes(provider)...)
	if !models.ChatOnlyProviders[provider] {
		// A failed check-in read is already recorded on its own row and must not
		// stop the credit read of the same account.
		if err := c.collectCheckin(ctx, state, provider, accountID, credential, credentialErr); err != nil && !isMetricFailure(err) {
			return accountResult{}, err
		}
	}
	// The credit read is keyed by account rather than by purpose, so it also
	// runs for an account whose purpose rows are all disabled: a manual refresh
	// of a revoked account still has to report what happened.
	points, pointsErr := c.collectPoints(ctx, state, provider, accountID, credential, credentialErr)
	return accountResult{Points: points, PointsErr: pointsErr}, nil
}

// collectCheckin records the upstream check-in status for one account.
//
// The stored value merges the upstream payload with the local day markers the
// console's check-in views read, so one snapshot answers both "what does the
// upstream say" and "did today's batch reach a terminal outcome". The local
// markers are written last and therefore win, because the upstream body
// describes the account, not which local day this deployment already settled.
func (c *Collector) collectCheckin(ctx context.Context, state *collectState, provider, accountID string, credential Credential, credentialErr error) error {
	key := metricKey{provider, accountID, KindCheckin}
	if c.inBackoff(key) {
		return c.writeBackoff(ctx, state, key)
	}
	localDate := store.LocalDate(c.opts.Settings.CheckinTimezone, c.clock())
	value := map[string]any{"local_date": localDate, "terminal_outcome": nil}
	status := statusUnknown
	daily, err := c.opts.DB.GetCheckinDailyState(ctx, provider, accountID, localDate, c.opts.Settings.CheckinTimezone)
	switch {
	case err == nil:
		status = statusFresh
		value["terminal_outcome"] = daily.TerminalOutcome
	case !errors.Is(err, store.ErrNotFound):
		return err
	}
	if credentialErr != nil {
		return c.writeFailure(ctx, state, key, credentialErr.Error())
	}
	upstream, err := c.opts.Clients.FetchCheckinStatus(ctx, provider, accountID, credential)
	if err != nil {
		return c.writeFailure(ctx, state, key, err.Error())
	}
	for name, item := range upstream {
		if name != "local_date" && name != "terminal_outcome" {
			value[name] = item
		}
	}
	if len(upstream) > 0 {
		status = statusFresh
	}
	return c.write(ctx, state, key, value, status, nil, nil)
}

// collectPoints records the provider's credit aggregate and returns the payload
// as stored.
func (c *Collector) collectPoints(ctx context.Context, state *collectState, provider, accountID string, credential Credential, credentialErr error) (map[string]any, error) {
	if !hasCreditRead(provider) {
		return nil, nil
	}
	key := metricKey{provider, accountID, KindPoints}
	if c.inBackoff(key) {
		return nil, c.writeBackoff(ctx, state, key)
	}
	if credentialErr != nil {
		return nil, c.writeFailure(ctx, state, key, credentialErr.Error())
	}
	value, err := c.opts.Clients.FetchCredits(ctx, provider, accountID, credential)
	if err != nil {
		return nil, c.writeFailure(ctx, state, key, err.Error())
	}
	if len(value) == 0 {
		// An empty aggregate means upstream answered without a usable balance;
		// storing it would blank the credits page.
		return nil, c.writeFailure(ctx, state, key, "empty credits response")
	}
	if err := c.write(ctx, state, key, value, statusFresh, nil, nil); err != nil {
		return nil, err
	}
	return value, nil
}

// enabledPurposes lists one account's enabled purpose rows.
func (c *Collector) enabledPurposes(ctx context.Context, provider, accountID string) ([]store.Purpose, error) {
	rows, err := c.opts.DB.ListPurposes(ctx, provider, accountID)
	if err != nil {
		return nil, err
	}
	out := make([]store.Purpose, 0, len(rows))
	for _, row := range rows {
		if row.Enabled {
			out = append(out, row)
		}
	}
	return out, nil
}

// resolveCredential decrypts the auth material for one account.
//
// The check-in purpose is preferred where the provider has one because that is
// the credential the billing endpoints were issued for; the chat material is
// the fallback the Python resolver called inherit_chat. Providers that report
// every purpose but only populate one still resolve, which is why the error
// names the last thing tried rather than the first.
func (c *Collector) resolveCredential(ctx context.Context, provider, accountID string, purposes ...string) (Credential, error) {
	lastErr := errors.New("access token unavailable")
	for _, purpose := range purposes {
		record, err := c.opts.DB.GetCredential(ctx, provider, accountID, purpose)
		if err != nil {
			if !errors.Is(err, store.ErrNotFound) {
				return Credential{}, err
			}
			lastErr = fmt.Errorf("no %s credential for %s", purpose, accountID)
			continue
		}
		payload, err := c.opts.Vault.Decrypt(record.EncryptedPayload)
		if err != nil {
			lastErr = errors.New("credential could not be decrypted")
			continue
		}
		token := accessToken(payload)
		if token == "" {
			lastErr = errors.New("access token unavailable")
			continue
		}
		return Credential{Mode: record.Mode, AccessToken: token, Cookie: stringValue(payload["cookie"])}, nil
	}
	return Credential{}, lastErr
}

// credentialPurposes is the auth-material lookup order for one provider.
func credentialPurposes(provider string) []string {
	if models.ChatOnlyProviders[provider] {
		return []string{"chat"}
	}
	return []string{"checkin", "chat"}
}

// hasCreditRead reports whether a provider publishes a credit balance.
func hasCreditRead(provider string) bool {
	return provider == models.ProviderWorkBuddy || provider == models.ProviderWorkBuddyIntl
}

// accessToken pulls the bearer token out of a decrypted credential payload.
func accessToken(payload map[string]any) string {
	for _, name := range []string{"access_token", "device_token", "token"} {
		if token := stringValue(payload[name]); token != "" {
			return token
		}
	}
	return ""
}

func stringValue(value any) string {
	text, ok := value.(string)
	if !ok {
		return ""
	}
	return strings.TrimSpace(text)
}

// write upserts the snapshot and appends a history sample.
//
// The snapshot keeps the FULL payload because the credits detail page renders
// the packages array; the history sample drops it because that array is ~97% of
// the bytes and the trend chart reads only the scalar totals. That asymmetry is
// deliberate and is what keeps account_metric_history small.
//
// observedAt is resolved to now when the caller passes nil, and preserved when
// the caller re-publishes a prior value so a stale sample does not look fresh.
func (c *Collector) write(ctx context.Context, state *collectState, key metricKey, value any, status string, lastError, observedAt *string) error {
	state.seen[key] = true
	stamp := derefOrEmpty(observedAt)
	if stamp == "" {
		stamp = store.NowISO()
	}
	if err := c.opts.DB.UpsertMetricSnapshot(ctx, store.MetricSnapshot{
		Provider:   key.provider,
		AccountID:  key.account,
		MetricKind: key.kind,
		Value:      value,
		ObservedAt: stamp,
		Status:     status,
		LastError:  lastError,
	}); err != nil {
		return err
	}
	if value != nil {
		if err := c.opts.DB.InsertMetricHistory(ctx, store.MetricHistoryRow{
			Provider:   key.provider,
			AccountID:  key.account,
			MetricKind: key.kind,
			Value:      historyValue(value),
			ObservedAt: stamp,
			Status:     status,
		}); err != nil {
			return err
		}
	}
	state.counts[status]++
	// Only a fresh sample is a real read: the stale re-publishes made by the
	// failure and backoff paths must not reset the retry window they set.
	if status == statusFresh {
		c.clearBackoff(key)
	}
	return nil
}

// writeFailure records a failed read. A metric that already has a value keeps
// reporting it as stale — an old balance is more useful to the console than a
// blank cell — while a metric that never succeeded is marked unavailable.
func (c *Collector) writeFailure(ctx context.Context, state *collectState, key metricKey, cause string) error {
	prior, ok := state.previous[key]
	status, value := statusUnavailable, any(nil)
	var observedAt *string
	if ok && prior.Value != nil {
		status, value = statusStale, prior.Value
		observedAt = &prior.ObservedAt
	}
	if err := c.write(ctx, state, key, value, status, &cause, observedAt); err != nil {
		return err
	}
	c.recordBackoff(key)
	return &metricFailure{key: key, cause: cause}
}

// writeBackoff short-circuits a metric that is still inside its retry window:
// the prior value is re-published as stale so the console keeps showing it
// without another upstream round trip.
func (c *Collector) writeBackoff(ctx context.Context, state *collectState, key metricKey) error {
	prior, ok := state.previous[key]
	if !ok || prior.Value == nil {
		state.seen[key] = true
		state.counts[statusSkipped]++
		return &metricFailure{key: key, cause: backoffError}
	}
	cause, observedAt := backoffError, prior.ObservedAt
	if err := c.write(ctx, state, key, prior.Value, statusStale, &cause, &observedAt); err != nil {
		return err
	}
	return &metricFailure{key: key, cause: backoffError}
}

// countUnseen counts every previously known metric this round did not touch,
// which is how an account that dropped out of the eligible set shows up in the
// tally instead of silently disappearing.
func (c *Collector) countUnseen(state *collectState) {
	for key := range state.previous {
		if !state.seen[key] {
			state.counts[statusSkipped]++
		}
	}
}

// prune drops history samples past the retention window.
func (c *Collector) prune(ctx context.Context) error {
	retention := c.opts.Settings.MetricsHistoryRetention
	if retention <= 0 {
		return nil
	}
	before := store.FormatISO(c.clock().AddDate(0, 0, -retention))
	_, err := c.opts.DB.PruneMetricHistory(ctx, before)
	return err
}

// BackoffSnapshot renders the per-metric retry state for the console, sorted by
// metric so the list is stable between reads.
func (c *Collector) BackoffSnapshot() []map[string]any {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]map[string]any, 0, len(c.backoff))
	for key, entry := range c.backoff {
		out = append(out, map[string]any{
			"metric":   key.String(),
			"attempts": entry.attempts,
			"retry_at": store.FormatISO(entry.retryAt),
		})
	}
	sort.Slice(out, func(i, j int) bool {
		return out[i]["metric"].(string) < out[j]["metric"].(string)
	})
	return out
}

// inBackoff reports whether a metric is still inside its retry window.
func (c *Collector) inBackoff(key metricKey) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	entry, ok := c.backoff[key]
	return ok && c.clock().Before(entry.retryAt)
}

// recordBackoff extends a metric's retry window by one consecutive failure.
func (c *Collector) recordBackoff(key metricKey) {
	c.mu.Lock()
	defer c.mu.Unlock()
	entry := c.backoff[key]
	entry.attempts++
	entry.retryAt = c.clock().Add(backoffDelay(entry.attempts))
	c.backoff[key] = entry
}

// clearBackoff forgets a metric's failures, so a metric that recovers is
// sampled at the normal cadence again instead of staying on the slow path.
func (c *Collector) clearBackoff(key metricKey) {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.backoff, key)
}

// backoffDelay grows the retry window exponentially from 30s to a one-hour cap,
// so a persistently broken metric costs one attempt per hour rather than one
// per round.
func backoffDelay(attempts int) time.Duration {
	exponent := attempts - 1
	switch {
	case exponent < 0:
		exponent = 0
	case exponent > 6:
		exponent = 6
	}
	delay := 30 * time.Second * time.Duration(1<<uint(exponent))
	if delay > time.Hour {
		return time.Hour
	}
	return delay
}

// clock reads the collector's time source.
func (c *Collector) clock() time.Time {
	if c.now == nil {
		return time.Now()
	}
	return c.now()
}

// tokenStatus derives a credential's state from its expiry instant.
func tokenStatus(expiresAt *string, now time.Time) string {
	if expiresAt == nil || strings.TrimSpace(*expiresAt) == "" {
		return statusUnknown
	}
	expiry, ok := store.ParseISO(*expiresAt)
	if !ok {
		return statusUnknown
	}
	if !expiry.After(now) {
		return statusExpired
	}
	return statusValid
}

// historyValue drops the packages array from a history sample.
//
// The decoded value is always the generic JSON tree the store produces, so the
// array is a []any; a payload built in-process is normalised to the same shape
// before it reaches here.
func historyValue(value any) any {
	object, ok := value.(map[string]any)
	if !ok {
		return value
	}
	if _, present := object["packages"].([]any); !present {
		return value
	}
	trimmed := make(map[string]any, len(object)-1)
	for name, item := range object {
		if name != "packages" {
			trimmed[name] = item
		}
	}
	return trimmed
}

func derefOrEmpty(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}
