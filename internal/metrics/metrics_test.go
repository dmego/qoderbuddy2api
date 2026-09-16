package metrics

import (
	"context"
	"errors"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/config"
	"github.com/dmego/qoderbuddy2api/internal/models"
	"github.com/dmego/qoderbuddy2api/internal/store"
	"github.com/dmego/qoderbuddy2api/internal/vault"
)

// testVaultKey is a fixed Fernet key so a test's encrypted payloads survive
// across collectors built inside one test.
const testVaultKey = "6vBwZHrTkLmNpQrStUvWxYz0123456789abcdefABCD="

// fakeClients records the calls it received and returns scripted results.
type fakeClients struct {
	credits    map[string]any
	checkin    map[string]any
	creditsErr error
	checkinErr error
	calls      []string
}

func (f *fakeClients) FetchCredits(_ context.Context, provider, accountID string, _ Credential) (map[string]any, error) {
	f.calls = append(f.calls, "credits:"+provider+"/"+accountID)
	if f.creditsErr != nil {
		return nil, f.creditsErr
	}
	return f.credits, nil
}

func (f *fakeClients) FetchCheckinStatus(_ context.Context, provider, accountID string, _ Credential) (map[string]any, error) {
	f.calls = append(f.calls, "checkin:"+provider+"/"+accountID)
	if f.checkinErr != nil {
		return nil, f.checkinErr
	}
	return f.checkin, nil
}

// fakeAccounts is a fixed account list.
type fakeAccounts struct {
	byProvider map[string][]AccountRef
	rebuilds   int
}

func (f *fakeAccounts) EligibleAccounts(provider string) []AccountRef { return f.byProvider[provider] }
func (f *fakeAccounts) Rebuild(context.Context) error                 { f.rebuilds++; return nil }

// newTestCollector builds a collector over a fresh SQLite database with one
// chat credential for the given provider.
func newTestCollector(t *testing.T, clients ProviderClients, accounts AccountLister, payload map[string]any, expiresAt *string) (*Collector, *store.DB) {
	t.Helper()
	db, err := store.Open(filepath.Join(t.TempDir(), "metrics.sqlite3"))
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	credVault, err := vault.New(testVaultKey)
	if err != nil {
		t.Fatalf("build vault: %v", err)
	}
	provider, accountID := "codebuddy", "cb-test"
	if err := seedAccount(t, db, credVault, provider, accountID, "chat", payload, expiresAt); err != nil {
		t.Fatalf("seed account: %v", err)
	}
	collector := NewCollector(CollectorOptions{
		DB:       db,
		Vault:    credVault,
		Settings: config.Settings{CheckinTimezone: "Asia/Shanghai", MetricsHistoryRetention: 90},
		Accounts: accounts,
		Clients:  clients,
	})
	return collector, db
}

// seedAccount writes one account, its purpose row, and an encrypted credential.
func seedAccount(t *testing.T, db *store.DB, credVault *vault.Vault, provider, accountID, purpose string, payload map[string]any, expiresAt *string) error {
	t.Helper()
	ctx := context.Background()
	if _, err := db.UpsertAccount(ctx, store.Account{Provider: provider, AccountID: accountID, Label: accountID, Source: "manual", Enabled: true}); err != nil {
		return err
	}
	if err := db.UpsertPurpose(ctx, store.Purpose{
		Provider: provider, AccountID: accountID, Purpose: purpose,
		Enabled: true, Status: "active", VerificationStatus: "verified", ExpiresAt: expiresAt,
	}); err != nil {
		return err
	}
	blob, err := credVault.Encrypt(payload)
	if err != nil {
		return err
	}
	_, err = db.UpsertCredential(ctx, store.CredentialWrite{
		Provider: provider, AccountID: accountID, Purpose: purpose,
		Mode: "bearer", EncryptedPayload: blob, ExpiresAt: expiresAt,
	})
	return err
}

// creditPayload is a representative points aggregate with a packages array.
func creditPayload() map[string]any {
	return map[string]any{
		"unit":            "credits",
		"total_remaining": 120,
		"total_used":      30,
		"packages": []any{
			map[string]any{"name": "积分包 1", "remaining": 120, "used": 30, "total": 150, "unit": "credits"},
			map[string]any{"name": "积分包 2", "remaining": 0, "used": 10, "total": 10, "unit": "credits"},
		},
	}
}

// TestHistoryDropsPackagesWhileSnapshotKeepsThem pins the storage asymmetry:
// the credits detail page renders packages from the snapshot, while the trend
// chart reads only scalar totals from a history table that must stay small.
func TestHistoryDropsPackagesWhileSnapshotKeepsThem(t *testing.T) {
	clients := &fakeClients{credits: creditPayload()}
	accounts := &fakeAccounts{byProvider: map[string][]AccountRef{
		models.ProviderWorkBuddy: {{Provider: models.ProviderWorkBuddy, AccountID: "cb-test"}},
	}}
	collector, db := newTestCollector(t, clients, accounts, map[string]any{"access_token": "token-value"}, nil)

	if _, err := collector.Collect(context.Background()); err != nil {
		t.Fatalf("collect: %v", err)
	}

	snapshot := findSnapshot(t, db, models.ProviderWorkBuddy, "cb-test", KindPoints)
	value, ok := snapshot.Value.(map[string]any)
	if !ok {
		t.Fatalf("snapshot value is %T, want object", snapshot.Value)
	}
	packages, ok := value["packages"].([]any)
	if !ok || len(packages) != 2 {
		t.Fatalf("snapshot packages = %#v, want 2 entries", value["packages"])
	}

	history := findHistory(t, db, models.ProviderWorkBuddy, "cb-test", KindPoints)
	historyValue, ok := history.Value.(map[string]any)
	if !ok {
		t.Fatalf("history value is %T, want object", history.Value)
	}
	if _, present := historyValue["packages"]; present {
		t.Fatalf("history row kept packages: %#v", historyValue)
	}
	// Every scalar the trend chart reads must survive the strip.
	for _, key := range []string{"unit", "total_remaining", "total_used"} {
		if historyValue[key] != value[key] {
			t.Fatalf("history %s = %#v, want %#v", key, historyValue[key], value[key])
		}
	}
}

// TestBackoffGrowsOnFailureAndResetsOnSuccess pins the retry policy: a broken
// metric must not be re-attempted every round, and a recovered metric must
// return to the normal cadence.
func TestBackoffGrowsOnFailureAndResetsOnSuccess(t *testing.T) {
	clients := &fakeClients{credits: creditPayload(), creditsErr: errors.New("transport:timeout")}
	accounts := &fakeAccounts{byProvider: map[string][]AccountRef{
		models.ProviderWorkBuddy: {{Provider: models.ProviderWorkBuddy, AccountID: "cb-test"}},
	}}
	collector, db := newTestCollector(t, clients, accounts, map[string]any{"access_token": "token-value"}, nil)
	now := time.Now()
	collector.now = func() time.Time { return now }
	key := metricKey{models.ProviderWorkBuddy, "cb-test", KindPoints}

	// Seed a successful sample so the failure path has a prior value to
	// re-publish, which is what the console renders while a metric is down.
	clients.creditsErr = nil
	if _, err := collector.Collect(context.Background()); err != nil {
		t.Fatalf("seed collect: %v", err)
	}

	// The first failure records one attempt and blocks the immediate retry. The
	// round itself still succeeds: a per-metric failure is recorded on the
	// metric's own row rather than failing the whole refresh.
	clients.creditsErr = errors.New("transport:timeout")
	counts, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	if counts[statusStale] == 0 {
		t.Fatalf("counts = %#v, want the failed metric counted stale", counts)
	}
	first := backoffFor(t, collector, key)
	if first.attempts != 1 {
		t.Fatalf("attempts after one failure = %d, want 1", first.attempts)
	}
	if !collector.inBackoff(key) {
		t.Fatal("metric is not in backoff immediately after a failure")
	}
	if status := findSnapshot(t, db, models.ProviderWorkBuddy, "cb-test", KindPoints).Status; status != statusStale {
		t.Fatalf("status after failure = %q, want %q", status, statusStale)
	}

	// A round inside the window must not reach the upstream again.
	before := creditsCalls(clients)
	if _, err := collector.Collect(context.Background()); err != nil {
		t.Fatalf("collect: %v", err)
	}
	if creditsCalls(clients) != before {
		t.Fatalf("credit read ran %d times inside the backoff window, want 0", creditsCalls(clients)-before)
	}

	// Past the window the metric is retried and the window lengthens.
	now = now.Add(31 * time.Second)
	if _, err := collector.Collect(context.Background()); err != nil {
		t.Fatalf("collect: %v", err)
	}
	second := backoffFor(t, collector, key)
	if second.attempts != 2 {
		t.Fatalf("attempts after two failures = %d, want 2", second.attempts)
	}
	if !second.retryAt.After(first.retryAt) {
		t.Fatalf("retry window did not grow: first %s, second %s", first.retryAt, second.retryAt)
	}

	// A success clears the backoff and samples normally again.
	clients.creditsErr = nil
	now = now.Add(time.Hour)
	if _, err := collector.Collect(context.Background()); err != nil {
		t.Fatalf("collect: %v", err)
	}
	if collector.inBackoff(key) {
		t.Fatal("successful read left the metric in backoff")
	}
	if status := findSnapshot(t, db, models.ProviderWorkBuddy, "cb-test", KindPoints).Status; status != statusFresh {
		t.Fatalf("status after recovery = %q, want %q", status, statusFresh)
	}
}

// TestTokenSnapshotUsesPurposeExpiry covers the token metric, which is derived
// from the purpose row and never calls upstream.
func TestTokenSnapshotUsesPurposeExpiry(t *testing.T) {
	clients := &fakeClients{credits: creditPayload()}
	accounts := &fakeAccounts{byProvider: map[string][]AccountRef{
		models.ProviderWorkBuddy: {{Provider: models.ProviderWorkBuddy, AccountID: "cb-test"}},
	}}
	expired := "2020-01-01T00:00:00+00:00"
	collector, db := newTestCollector(t, clients, accounts, map[string]any{"access_token": "token-value"}, &expired)

	if _, err := collector.Collect(context.Background()); err != nil {
		t.Fatalf("collect: %v", err)
	}
	snapshot := findSnapshot(t, db, models.ProviderWorkBuddy, "cb-test", KindTokenChat)
	if snapshot.Status != statusExpired {
		t.Fatalf("token status = %q, want %q", snapshot.Status, statusExpired)
	}
}

// TestRefreshAllRecordsOperation pins the operation row the console polls.
func TestRefreshAllRecordsOperation(t *testing.T) {
	clients := &fakeClients{credits: creditPayload()}
	accounts := &fakeAccounts{byProvider: map[string][]AccountRef{
		models.ProviderWorkBuddy: {{Provider: models.ProviderWorkBuddy, AccountID: "cb-test"}},
	}}
	collector, db := newTestCollector(t, clients, accounts, map[string]any{"access_token": "token-value"}, nil)

	collector.RefreshAll(context.Background(), "mr-test")

	operation, err := db.GetMetricRefreshOperation(context.Background(), "mr-test")
	if err != nil {
		t.Fatalf("read operation: %v", err)
	}
	if operation.Status != operationSucceeded {
		t.Fatalf("operation status = %q, want %q", operation.Status, operationSucceeded)
	}
	if operation.FinishedAt == nil {
		t.Fatal("operation has no finished_at")
	}
	result, ok := operation.Result.(map[string]any)
	if !ok || result[statusFresh] == nil {
		t.Fatalf("operation result = %#v, want a fresh count", operation.Result)
	}
}

// TestIntlAccountReadsCreditsWithoutCheckin covers the international provider,
// which has no check-in purpose and no check-in endpoint.
func TestIntlAccountReadsCreditsWithoutCheckin(t *testing.T) {
	clients := &fakeClients{credits: creditPayload()}
	accounts := &fakeAccounts{byProvider: map[string][]AccountRef{
		models.ProviderWorkBuddyIntl: {{Provider: models.ProviderWorkBuddyIntl, AccountID: "intl-test"}},
	}}
	db, err := store.Open(filepath.Join(t.TempDir(), "intl.sqlite3"))
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	credVault, err := vault.New(testVaultKey)
	if err != nil {
		t.Fatalf("build vault: %v", err)
	}
	if err := seedAccount(t, db, credVault, models.ProviderWorkBuddyIntl, "intl-test", "chat", map[string]any{"access_token": "token-value"}, nil); err != nil {
		t.Fatalf("seed account: %v", err)
	}
	collector := NewCollector(CollectorOptions{
		DB: db, Vault: credVault,
		Settings: config.Settings{CheckinTimezone: "Asia/Shanghai"},
		Accounts: accounts, Clients: clients,
	})

	if _, err := collector.Collect(context.Background()); err != nil {
		t.Fatalf("collect: %v", err)
	}
	if len(findSnapshots(db, t, models.ProviderWorkBuddyIntl, "intl-test", KindCheckin)) != 0 {
		t.Fatal("international account got a check-in snapshot")
	}
	if len(findSnapshots(db, t, models.ProviderWorkBuddyIntl, "intl-test", KindPoints)) != 1 {
		t.Fatal("international account got no points snapshot")
	}
	if len(findSnapshots(db, t, models.ProviderWorkBuddyIntl, "intl-test", KindTokenChat)) != 1 {
		t.Fatal("international account got no chat token snapshot")
	}
}

// TestRefreshOneReturnsPackagesForTheCheckinPipeline pins the contract the
// check-in pipeline depends on: after a claim it re-reads this payload to
// compute the reward delta, so the packages array must come back intact.
func TestRefreshOneReturnsPackagesForTheCheckinPipeline(t *testing.T) {
	clients := &fakeClients{credits: creditPayload()}
	accounts := &fakeAccounts{}
	collector, db := newTestCollector(t, clients, accounts, map[string]any{"access_token": "token-value"}, nil)

	value, err := collector.RefreshOne(context.Background(), models.ProviderWorkBuddy, "cb-test")
	if err != nil {
		t.Fatalf("refresh one: %v", err)
	}
	if len(packageRows(t, value)) != 2 {
		t.Fatalf("returned packages = %#v", value["packages"])
	}
	if value["total_remaining"] != 120 {
		t.Fatalf("total_remaining = %#v", value["total_remaining"])
	}
	// The same payload must have been persisted, so the check-in pipeline's
	// later snapshot read sees the same balance.
	stored, ok := findSnapshot(t, db, models.ProviderWorkBuddy, "cb-test", KindPoints).Value.(map[string]any)
	if !ok || len(packageRows(t, stored)) != 2 {
		t.Fatalf("stored snapshot = %#v", stored)
	}
	// And the history row must still be stripped.
	if _, present := findHistory(t, db, models.ProviderWorkBuddy, "cb-test", KindPoints).Value.(map[string]any)["packages"]; present {
		t.Fatal("history row from a per-account refresh kept packages")
	}
}

// TestFailedCheckinReadDoesNotSuppressCreditRead pins metric independence: the
// two reads of one account fail and succeed separately, so a check-in outage
// must not blank the credits page.
func TestFailedCheckinReadDoesNotSuppressCreditRead(t *testing.T) {
	clients := &fakeClients{
		credits:    creditPayload(),
		checkinErr: errCreditsDown,
	}
	accounts := &fakeAccounts{byProvider: map[string][]AccountRef{
		models.ProviderWorkBuddy: {{Provider: models.ProviderWorkBuddy, AccountID: "cb-test"}},
	}}
	collector, db := newTestCollector(t, clients, accounts, map[string]any{"access_token": "token-value"}, nil)

	counts, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	if counts[statusFresh] == 0 {
		t.Fatalf("counts = %#v, want the credit metric counted fresh", counts)
	}
	if status := findSnapshot(t, db, models.ProviderWorkBuddy, "cb-test", KindPoints).Status; status != statusFresh {
		t.Fatalf("points status = %q, want %q", status, statusFresh)
	}
	if status := findSnapshot(t, db, models.ProviderWorkBuddy, "cb-test", KindCheckin).Status; status != statusUnavailable {
		t.Fatalf("checkin status = %q, want %q", status, statusUnavailable)
	}
}

// errCreditsDown stands in for an unreachable billing endpoint.
var errCreditsDown = errors.New("transport:timeout")

// waitFor polls a condition with a deadline, so a scheduler test does not rely
// on a fixed sleep.
func waitFor(t *testing.T, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if condition() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("condition was not met before the deadline")
}

// creditsCalls counts the credit reads issued so far, so a backoff assertion is
// not confused by the check-in read of the same round.
func creditsCalls(clients *fakeClients) int {
	count := 0
	for _, call := range clients.calls {
		if strings.HasPrefix(call, "credits:") {
			count++
		}
	}
	return count
}

func backoffFor(t *testing.T, collector *Collector, key metricKey) backoffEntry {
	t.Helper()
	collector.mu.Lock()
	defer collector.mu.Unlock()
	entry, ok := collector.backoff[key]
	if !ok {
		t.Fatalf("no backoff entry for %s", key)
	}
	return entry
}

func findSnapshot(t *testing.T, db *store.DB, provider, accountID, kind string) store.MetricSnapshot {
	t.Helper()
	rows := findSnapshots(db, t, provider, accountID, kind)
	if len(rows) != 1 {
		t.Fatalf("snapshot rows for %s/%s/%s = %d, want 1", provider, accountID, kind, len(rows))
	}
	return rows[0]
}

func findSnapshots(db *store.DB, t *testing.T, provider, accountID, kind string) []store.MetricSnapshot {
	t.Helper()
	rows, err := db.ListMetricSnapshots(context.Background(), provider, accountID)
	if err != nil {
		t.Fatalf("list snapshots: %v", err)
	}
	out := []store.MetricSnapshot{}
	for _, row := range rows {
		if row.MetricKind == kind {
			out = append(out, row)
		}
	}
	return out
}

func findHistory(t *testing.T, db *store.DB, provider, accountID, kind string) store.MetricHistoryRow {
	t.Helper()
	rows, err := db.ListMetricHistory(context.Background(), provider, accountID, kind, "", 10)
	if err != nil {
		t.Fatalf("list history: %v", err)
	}
	if len(rows) == 0 {
		t.Fatalf("no history row for %s/%s/%s", provider, accountID, kind)
	}
	return rows[len(rows)-1]
}
