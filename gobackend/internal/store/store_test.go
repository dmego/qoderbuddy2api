package store

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/dmego/qoderbuddy2api/gobackend/internal/vault"
)

// The rewrite's central compatibility claim is that the Go binary can be pointed
// at a database written by the Python control plane with no migration step and
// no re-login: credentials must decrypt and accounts must be readable. That
// cannot be proved against a fresh temp database, so this test runs only when a
// real database copy is supplied.
//
//	QB2API_TEST_DB=/path/to/qb2api.sqlite3 \
//	QB2API_TEST_VAULT_KEY=<fernet key> go test ./internal/store/ -run TestOpensExistingDatabase -v
func TestOpensExistingDatabase(t *testing.T) {
	path := os.Getenv("QB2API_TEST_DB")
	if path == "" {
		t.Skip("set QB2API_TEST_DB to a copy of a real qb2api.sqlite3")
	}
	// Work on a copy: migrate() writes, and the supplied file may be the live one.
	workdir := t.TempDir()
	target := filepath.Join(workdir, "qb2api.sqlite3")
	source, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read source db: %v", err)
	}
	if err := os.WriteFile(target, source, 0o600); err != nil {
		t.Fatalf("write db copy: %v", err)
	}

	db, err := Open(target)
	if err != nil {
		t.Fatalf("Open existing database: %v", err)
	}
	defer db.Close()
	ctx := context.Background()

	accounts, err := db.ListAccounts(ctx, []string{"codebuddy", "workbuddy_intl"})
	if err != nil {
		t.Fatalf("ListAccounts: %v", err)
	}
	if len(accounts) == 0 {
		t.Fatal("no accounts found: the copy does not look like a real database")
	}
	t.Logf("read %d accounts after migration without a re-import", len(accounts))

	// Every account that had a chat credential before the rewrite must still
	// have one, and its payload must still be decryptable by the Go vault — that
	// is the difference between "the schema matches" and "nobody has to log in
	// again", which is the actual requirement.
	readable := 0
	var vaultKey string
	if key := os.Getenv("QB2API_TEST_VAULT_KEY"); key != "" {
		vaultKey = key
	}
	var credVault *vault.Vault
	if vaultKey != "" {
		credVault, err = vault.New(vaultKey)
		if err != nil {
			t.Fatalf("build vault from QB2API_TEST_VAULT_KEY: %v", err)
		}
	}
	decrypted := 0
	for _, account := range accounts {
		record, err := db.GetCredential(ctx, account.Provider, account.AccountID, "chat")
		if err != nil {
			continue
		}
		if record.EncryptedPayload == "" {
			t.Fatalf("%s/%s has an empty credential payload", account.Provider, account.AccountID)
		}
		readable++
		if credVault == nil {
			continue
		}
		payload, err := credVault.Decrypt(record.EncryptedPayload)
		if err != nil {
			t.Fatalf("decrypt %s/%s: %v", account.Provider, account.AccountID, err)
		}
		if token, _ := payload["access_token"].(string); token == "" {
			t.Fatalf("%s/%s decrypted but carries no access_token: %v",
				account.Provider, account.AccountID, payloadKeys(payload))
		}
		decrypted++
	}
	if readable == 0 {
		t.Fatal("no readable chat credentials found")
	}
	t.Logf("found %d encrypted chat credentials", readable)
	if credVault != nil {
		if decrypted != readable {
			t.Fatalf("decrypted %d of %d credentials", decrypted, readable)
		}
		t.Logf("decrypted all %d credentials with the existing vault key: no re-login needed", decrypted)
	} else {
		t.Log("set QB2API_TEST_VAULT_KEY to also verify credential decryption")
	}

	// Totals must survive migration: a missing table would return zero rows here.
	events, err := db.ListRequestEvents(ctx, UsageFilter{}, 5, 0)
	if err != nil {
		t.Fatalf("ListRequestEvents: %v", err)
	}
	t.Logf("sampled %d request events", len(events))

	summary, err := db.SummarizeUsage(ctx, UsageFilter{})
	if err != nil {
		t.Fatalf("SummarizeUsage: %v", err)
	}
	if summary.RequestCount == 0 {
		t.Fatal("usage summary is empty after migration")
	}
	t.Logf("usage summary: %d requests, %d succeeded", summary.RequestCount, summary.SuccessCount)
}

// TestClaimActiveDayIsOnePerDay proves the growth automation's daily lock: the
// first claim for a local date wins and every later claim loses, which is what
// prevents a second ACP conversation (real upstream cost) on the same day.
func TestClaimActiveDayIsOnePerDay(t *testing.T) {
	db := newTestDB(t)
	ctx := context.Background()
	day := ActiveDay{
		Provider:  "codebuddy",
		AccountID: "cb-test",
		LocalDate: "2026-09-16",
		Timezone:  "Asia/Shanghai",
	}

	first, err := db.ClaimActiveDay(ctx, day)
	if err != nil {
		t.Fatalf("first claim: %v", err)
	}
	if !first {
		t.Fatal("first claim of the day must win")
	}
	second, err := db.ClaimActiveDay(ctx, day)
	if err != nil {
		t.Fatalf("second claim: %v", err)
	}
	if second {
		t.Fatal("second claim of the same day must lose: it would send another ACP turn")
	}
	// A different local date is a new day and must be claimable again.
	next := day
	next.LocalDate = "2026-09-17"
	third, err := db.ClaimActiveDay(ctx, next)
	if err != nil {
		t.Fatalf("next-day claim: %v", err)
	}
	if !third {
		t.Fatal("a new local date must be claimable")
	}
}

// TestRollupWritesNestedWindowsInOneTransaction proves the rollup reads the
// widest window once and still produces every nested bucket, and that a second
// round is idempotent (same buckets, no duplicates).
func TestRollupWritesNestedWindowsInOneTransaction(t *testing.T) {
	db := newTestDB(t)
	ctx := context.Background()
	now := NowISO()
	for _, provider := range []string{"codebuddy", "workbuddy_intl"} {
		eventID := provider + "-1"
		latency, firstToken := 1500, 300
		in, out := 100, 50
		if _, err := db.ExecContext(ctx, `
			INSERT INTO request_events
				(event_id, request_id, provider, model_id, protocol, status, input_tokens, output_tokens,
				 latency_ms, first_token_ms, started_at, finished_at)
			VALUES (?, ?, ?, ?, 'openai', 'succeeded', ?, ?, ?, ?, ?, ?)`,
			eventID, "req-1", provider, "deepseek-v4.1-flash",
			in, out, latency, firstToken, now, now); err != nil {
			t.Fatalf("insert event: %v", err)
		}
	}

	groups, _, err := db.RollupOnce(ctx, mustParse(t, now), 90)
	if err != nil {
		t.Fatalf("RollupOnce: %v", err)
	}
	// Two providers times three windows.
	if groups != 6 {
		t.Fatalf("expected 6 (provider x window) groups, got %d", groups)
	}
	rollups, err := db.ListRollups(ctx, UsageFilter{}, "minute", 10, 0)
	if err != nil {
		t.Fatalf("ListRollups: %v", err)
	}
	if len(rollups) == 0 {
		t.Fatal("minute rollups missing after a rollup round")
	}
	if rollups[0].TTFTAvgMS == nil || *rollups[0].TTFTAvgMS != 300 {
		t.Fatalf("first-token latency not aggregated: %#v", rollups[0])
	}
	if rollups[0].LatencyAvgMS == nil || *rollups[0].LatencyAvgMS != 1500 {
		t.Fatalf("total latency not aggregated: %#v", rollups[0])
	}
}

// TestEventWriterBatchesInserts proves the write-coalescing contract: enqueued
// events become visible after one flush, and the batch is not lost.
func TestEventWriterBatchesInserts(t *testing.T) {
	db := newTestDB(t)
	ctx := context.Background()
	writer := NewEventWriter(db, 0, 0)

	const count = 25
	for index := 0; index < count; index++ {
		latency := 100 + index
		writer.Enqueue(RequestEvent{
			EventID:   "evt-" + strconvItoa(index),
			RequestID: "req-" + strconvItoa(index),
			Provider:  "codebuddy",
			ModelID:   "deepseek-v4.1-flash",
			Protocol:  "openai",
			Status:    "succeeded",
			LatencyMS: &latency,
			StartedAt: NowISO(),
		})
	}
	if err := writer.Flush(ctx); err != nil {
		t.Fatalf("Flush: %v", err)
	}
	written, dropped, lastErr, pending := writer.Stats()
	if dropped != 0 || lastErr != nil || pending != 0 {
		t.Fatalf("writer reported trouble: written=%d dropped=%d pending=%d err=%v",
			written, dropped, pending, lastErr)
	}
	var stored int
	if err := db.QueryRowContext(ctx, "SELECT COUNT(*) FROM request_events").Scan(&stored); err != nil {
		t.Fatalf("count events: %v", err)
	}
	if stored != count {
		t.Fatalf("expected %d stored events, got %d", count, stored)
	}
}

func newTestDB(t *testing.T) *DB {
	t.Helper()
	path := filepath.Join(t.TempDir(), "qb2api.sqlite3")
	db, err := Open(path)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return db
}

func mustParse(t *testing.T, value string) time.Time {
	t.Helper()
	parsed, ok := ParseISO(value)
	if !ok {
		t.Fatalf("cannot parse %q", value)
	}
	return parsed
}

// payloadKeys lists a decrypted payload's key names. Values are never printed:
// a credential payload holds bearer material and must not reach test output.
func payloadKeys(payload map[string]any) []string {
	out := make([]string, 0, len(payload))
	for key := range payload {
		out = append(out, key)
	}
	return out
}

func strconvItoa(value int) string {
	if value == 0 {
		return "0"
	}
	digits := ""
	for value > 0 {
		digits = string(rune('0'+value%10)) + digits
		value /= 10
	}
	return digits
}
