package server

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/chatwire"
	"github.com/dmego/qoderbuddy2api/internal/models"
	"github.com/dmego/qoderbuddy2api/internal/providers"
	"github.com/dmego/qoderbuddy2api/internal/store"
)

// TestRefreshDueRollingTrigger is the regression for the outage that motivated
// this whole path: WorkBuddy hands out 28-day access tokens but revokes the
// session behind them within days, so the expiry-driven rule never fired and
// nothing refreshed a credential that upstream had already stopped honouring.
//
// Measured 2026-10-17: three of four accounts were refused 11 days after
// signing in while their stored expiry still read 2026-11-13.
func TestRefreshDueRollingTrigger(t *testing.T) {
	now := time.Date(2026, 10, 17, 12, 0, 0, 0, time.UTC)
	lifetime := 28 * 24 * time.Hour
	policy := rotationPolicy{interval: 6 * time.Hour, lead: 30 * time.Minute, fraction: defaultRotationFraction}

	expiresAt := now.Add(lifetime - 11*24*time.Hour) // the measured shape: 17 days left
	updatedAt := now.Add(-6 * time.Hour)             // refreshed by the rolling trigger

	if !policy.refreshDue(updatedAt, expiresAt, true, now) {
		t.Fatal("a credential last refreshed one full interval ago must be refreshed")
	}
	if policy.refreshDue(now.Add(-time.Hour), expiresAt, true, now) {
		t.Fatal("a credential refreshed an hour ago must be left alone")
	}
}

// TestRefreshDueTable pins the three triggers and the cases where each must stay
// silent, including the exact combination that used to never fire.
func TestRefreshDueTable(t *testing.T) {
	now := time.Date(2026, 10, 17, 12, 0, 0, 0, time.UTC)
	policy := rotationPolicy{interval: 6 * time.Hour, lead: 30 * time.Minute, fraction: 0.2}
	day := 24 * time.Hour

	cases := []struct {
		name      string
		updatedAt time.Time
		expiresAt time.Time
		hasExpiry bool
		want      bool
	}{
		{
			name:      "rolling interval elapsed",
			updatedAt: now.Add(-6 * time.Hour),
			expiresAt: now.Add(20 * day),
			hasExpiry: true,
			want:      true,
		},
		{
			name:      "rolling interval not elapsed and expiry far",
			updatedAt: now.Add(-time.Hour),
			expiresAt: now.Add(20 * day),
			hasExpiry: true,
			want:      false,
		},
		{
			// The historical rule: 30 minutes of lead against a 28-day token.
			// It is still supported, it just is not what carries the feature.
			name:      "absolute lead reached",
			updatedAt: now.Add(-time.Minute),
			expiresAt: now.Add(10 * time.Minute),
			hasExpiry: true,
			want:      true,
		},
		{
			name:      "absolute lead not reached",
			updatedAt: now.Add(-time.Minute),
			expiresAt: now.Add(2 * time.Hour),
			hasExpiry: true,
			want:      false,
		},
		{
			// A 2-hour token starts refreshing with 24 minutes left.
			name:      "proportional window on a short token",
			updatedAt: now.Add(-110 * time.Minute),
			expiresAt: now.Add(10 * time.Minute),
			hasExpiry: true,
			want:      true,
		},
		{
			name:      "proportional window not reached on a long token",
			updatedAt: now.Add(-time.Hour),
			expiresAt: now.Add(20 * day),
			hasExpiry: true,
			want:      false,
		},
		{
			// An unknown deadline is precisely the case where waiting for it
			// means waiting forever, so the rolling trigger alone decides.
			name:      "no expiry refreshed by rolling interval",
			updatedAt: now.Add(-7 * time.Hour),
			hasExpiry: false,
			want:      true,
		},
		{
			name:      "no expiry and recently refreshed",
			updatedAt: now.Add(-time.Hour),
			hasExpiry: false,
			want:      false,
		},
		{
			// A row whose expiry is at or before its write time has no lifetime
			// to take a fraction of. The proportional trigger must then stay
			// silent rather than fire on a negative window.
			name:      "zero lifetime does not fire the proportional trigger",
			updatedAt: now.Add(-time.Minute),
			expiresAt: now.Add(-time.Minute),
			hasExpiry: true,
			want:      false,
		},
		{
			// With the rolling trigger off, a 20-day token written an hour ago
			// is 20 days from expiry and 4 days from the proportional window.
			name:      "rolling interval disabled falls back to expiry",
			updatedAt: now.Add(-time.Hour),
			expiresAt: now.Add(20 * day),
			hasExpiry: true,
			want:      false,
		},
	}

	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			active := policy
			switch testCase.name {
			case "rolling interval disabled falls back to expiry":
				active.interval = 0
			case "zero lifetime does not fire the proportional trigger":
				// Isolate the proportional rule from the absolute lead.
				active.lead = 0
			}
			if got := active.refreshDue(testCase.updatedAt, testCase.expiresAt, testCase.hasExpiry, now); got != testCase.want {
				t.Fatalf("refreshDue = %v, want %v", got, testCase.want)
			}
		})
	}
}

// TestClassifyRefreshFailure pins the split that the retry policy depends on.
//
// The bare 12153 code is deliberately NOT treated as a dead session: measured
// 2026-10-06, the same token alternated between that body and success. Only the
// invalid_grant texts proved permanent (measured 2026-10-17, three accounts).
func TestClassifyRefreshFailure(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want refreshFailure
	}{
		{
			name: "revoked session behind the token",
			err:  errors.New(`workbuddy 401: 12153:refresh token failed:400 Bad Request: invalid_grant: Refresh token issued before the user session started`),
			want: refreshSessionDead,
		},
		{
			name: "offline session deleted",
			err:  errors.New(`workbuddy 401: 12153:refresh token failed:400 Bad Request: invalid_grant: Offline user session not found`),
			want: refreshSessionDead,
		},
		{
			name: "bare 12153 is transient",
			err:  errors.New(`workbuddy 401: 12153:refresh token is invalid`),
			want: refreshTransient,
		},
		{
			name: "upstream 5xx is transient",
			err:  errors.New(`workbuddy 502: upstream unavailable`),
			want: refreshTransient,
		},
		{
			name: "network fault is transient",
			err:  errors.New(`dial tcp 10.0.0.1:443: connect: connection refused`),
			want: refreshTransient,
		},
		{
			name: "missing refresh token is rejected",
			err:  errors.New(`no refresh token stored`),
			want: refreshRejected,
		},
		{
			name: "malformed payload is rejected",
			err:  errors.New(`auth_invalid_json: unexpected end of JSON input`),
			want: refreshRejected,
		},
		{
			name: "nil is not a failure",
			err:  nil,
			want: refreshTransient,
		},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			if got := classifyRefreshFailure(testCase.err); got != testCase.want {
				t.Fatalf("classifyRefreshFailure = %s, want %s", got, testCase.want)
			}
		})
	}
}

// TestRotateOnceMarksRevokedSessionAsNeedsReauth covers the state transition the
// console and the pool both read: a revoked session must leave the account out
// of rotation with a reason, instead of being retried as if it were transient.
func TestRotateOnceMarksRevokedSessionAsNeedsReauth(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = io.WriteString(w, `{"code":12153,"msg":"12153:refresh token failed:400 Bad Request: invalid_grant: Refresh token issued before the user session started"}`)
	}))
	defer server.Close()

	api, db := newTestAPI(t)
	seedRotatableAccount(t, db, api.Vault, models.ProviderWorkBuddy, "cb-revoked", "rt", "at")
	ctx := context.Background()

	round := rotateOnce(ctx, db, api.Vault, rotationPolicy{lead: 30 * time.Minute}, nil, rotationSettings(server.URL), map[string]bool{}, nil)

	purposes, err := db.ListPurposes(ctx, models.ProviderWorkBuddy, "cb-revoked")
	if err != nil {
		t.Fatalf("list purposes: %v", err)
	}
	if len(purposes) != 1 {
		t.Fatalf("purposes = %d, want 1", len(purposes))
	}
	if purposes[0].Status != "needs_reauth" {
		t.Fatalf("status = %q, want needs_reauth for a revoked session", purposes[0].Status)
	}
	if purposes[0].LastError == nil || *purposes[0].LastError != "session_revoked" {
		t.Fatalf("last_error = %v, want session_revoked", purposes[0].LastError)
	}
	// The credential itself must survive: re-login replaces it, and deleting the
	// row here would lose the account id the operator needs to re-import.
	if _, err := db.GetCredential(ctx, models.ProviderWorkBuddy, "cb-revoked", "chat"); err != nil {
		t.Fatalf("the credential row must survive a revoked session: %v", err)
	}
	// The next attempt must be spaced by the rolling interval, not the scan tick.
	key := models.ProviderWorkBuddy + ":cb-revoked:chat"
	delay, ok := round.retryAfter[key]
	if !ok || delay <= 0 {
		t.Fatalf("retryAfter[%s] = %v, want a positive delay", key, delay)
	}
}

// TestRotateOnceRetriesTransientAtScanCadence is the counterpart: a transient
// refusal must NOT be spaced out, because the next scan is likely to succeed.
func TestRotateOnceRetriesTransientAtScanCadence(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = io.WriteString(w, `{"code":12153,"msg":"12153:refresh token is invalid"}`)
	}))
	defer server.Close()

	api, db := newTestAPI(t)
	seedRotatableAccount(t, db, api.Vault, models.ProviderWorkBuddy, "cb-transient", "rt", "at")
	ctx := context.Background()

	round := rotateOnce(ctx, db, api.Vault, rotationPolicy{lead: 30 * time.Minute}, nil, rotationSettings(server.URL), map[string]bool{}, nil)

	if len(round.retryAfter) != 0 {
		t.Fatalf("retryAfter = %v, want no spacing for a transient failure", round.retryAfter)
	}
	purposes, err := db.ListPurposes(ctx, models.ProviderWorkBuddy, "cb-transient")
	if err != nil {
		t.Fatalf("list purposes: %v", err)
	}
	if purposes[0].Status != "active" {
		t.Fatalf("status = %q, want active: a bare 12153 is retryable", purposes[0].Status)
	}
}

// TestRotateOnceSkipsHeldCredential proves the spacing is honoured: a credential
// whose retry is not due yet must not generate an upstream call.
func TestRotateOnceSkipsHeldCredential(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"code":0,"data":{"accessToken":"a","expiresIn":3600}}`)
	}))
	defer server.Close()

	api, db := newTestAPI(t)
	seedRotatableAccount(t, db, api.Vault, models.ProviderWorkBuddy, "cb-held", "rt", "at")
	held := map[string]time.Time{
		models.ProviderWorkBuddy + ":cb-held:chat": time.Now().Add(time.Hour),
	}

	rotateOnce(context.Background(), db, api.Vault, rotationPolicy{lead: 30 * time.Minute}, nil, rotationSettings(server.URL), map[string]bool{}, held)

	if calls != 0 {
		t.Fatalf("refresh calls = %d, want 0 while the retry is held", calls)
	}
}

// TestRotateOnceRollsHealthyCredential is the end-to-end proof of the new
// trigger: a credential with 20 days left and no lead-window pressure must still
// be refreshed once the rolling interval has elapsed.
func TestRotateOnceRollsHealthyCredential(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"code":0,"data":{"accessToken":"rolled","refreshToken":"rolled-rt","expiresIn":2419200}}`)
	}))
	defer server.Close()

	api, db := newTestAPI(t)
	ctx := context.Background()
	if _, err := db.UpsertAccount(ctx, store.Account{Provider: models.ProviderWorkBuddy, AccountID: "cb-healthy", Label: "cb-healthy", Source: "oauth", Enabled: true}); err != nil {
		t.Fatalf("upsert account: %v", err)
	}
	if err := db.UpsertPurpose(ctx, store.Purpose{Provider: models.ProviderWorkBuddy, AccountID: "cb-healthy", Purpose: "chat", Enabled: true, Status: "active"}); err != nil {
		t.Fatalf("upsert purpose: %v", err)
	}
	blob, err := api.Vault.Encrypt(map[string]any{"access_token": "old", "refresh_token": "old-rt"})
	if err != nil {
		t.Fatalf("encrypt: %v", err)
	}
	far := store.FormatISO(time.Now().UTC().Add(20 * 24 * time.Hour))
	if _, err := db.UpsertCredential(ctx, store.CredentialWrite{
		Provider: models.ProviderWorkBuddy, AccountID: "cb-healthy", Purpose: "chat",
		Mode: "oauth", EncryptedPayload: blob, HasRefreshToken: true, ExpiresAt: &far,
	}); err != nil {
		t.Fatalf("upsert credential: %v", err)
	}

	// The stored row is brand new, so the rolling trigger is not due yet.
	rotateOnce(ctx, db, api.Vault, rotationPolicy{interval: 6 * time.Hour}, nil, rotationSettings(server.URL), map[string]bool{}, nil)
	if calls != 0 {
		t.Fatalf("refresh calls = %d, want 0 immediately after a write", calls)
	}

	// Backdate the row past one interval, the way a running process would age it.
	ageCredential(t, db, models.ProviderWorkBuddy, "cb-healthy", 7*time.Hour)

	rotateOnce(ctx, db, api.Vault, rotationPolicy{interval: 6 * time.Hour}, nil, rotationSettings(server.URL), map[string]bool{}, nil)
	if calls != 1 {
		t.Fatalf("refresh calls = %d, want 1 once the rolling interval elapsed", calls)
	}
	record, err := db.GetCredential(ctx, models.ProviderWorkBuddy, "cb-healthy", "chat")
	if err != nil {
		t.Fatalf("read credential: %v", err)
	}
	payload, err := api.Vault.Decrypt(record.EncryptedPayload)
	if err != nil {
		t.Fatalf("decrypt: %v", err)
	}
	if payload["access_token"] != "rolled" {
		t.Fatalf("access_token = %v, want the rolled value", payload["access_token"])
	}
}

// TestAuthRescueDeduplicatesRefusedTokens bounds the repair volume: a pool-wide
// 401 storm must produce one refresh per account per cooldown, not one per
// failed request.
func TestAuthRescueDeduplicatesRefusedTokens(t *testing.T) {
	var (
		mu    sync.Mutex
		calls int
	)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		calls++
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"code":0,"data":{"accessToken":"rescued","refreshToken":"rescued-rt","expiresIn":3600}}`)
	}))
	defer server.Close()

	api, db := newTestAPI(t)
	seedRotatableAccount(t, db, api.Vault, models.ProviderWorkBuddy, "cb-storm", "rt", "at")
	rescue := newAuthRescue(db, api.Vault, nil, rotationSettings(server.URL))

	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			rescue.trigger(models.ProviderWorkBuddy, "cb-storm")
		}()
	}
	wg.Wait()
	// trigger() launches the refresh in the background; wait for the one call.
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		done := calls > 0
		mu.Unlock()
		if done {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	time.Sleep(200 * time.Millisecond) // give any duplicate rescue a chance to fire
	mu.Lock()
	got := calls
	mu.Unlock()
	if got != 1 {
		t.Fatalf("refresh calls = %d, want exactly 1 for 20 concurrent 401 reports", got)
	}
}

// TestAuthRescueIgnoresProvidersWithoutContract pins the guard: a provider with
// no refresh endpoint must not trigger a rescue attempt.
func TestAuthRescueIgnoresProvidersWithoutContract(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		_, _ = io.WriteString(w, `{"code":0,"data":{"accessToken":"x","expiresIn":3600}}`)
	}))
	defer server.Close()

	api, db := newTestAPI(t)
	rescue := newAuthRescue(db, api.Vault, nil, rotationSettings(server.URL))
	rescue.trigger("qoder", "some-account")
	time.Sleep(100 * time.Millisecond)
	if calls != 0 {
		t.Fatalf("refresh calls = %d, want 0 for a provider without a refresh contract", calls)
	}
}

// TestPoolReportsRefusedToken proves the wiring between the pool and the repair
// hook: a 401 from a provider must reach the callback with the account id, and
// a non-401 failure must not.
func TestPoolReportsRefusedToken(t *testing.T) {
	api, db := newTestAPI(t)
	_ = api
	_ = db

	cases := []struct {
		name string
		err  error
		want bool
	}{
		{name: "401 reports", err: &providers.UpstreamError{StatusCode: http.StatusUnauthorized, Message: "token expired"}, want: true},
		{name: "502 stays silent", err: &providers.UpstreamError{StatusCode: http.StatusBadGateway, Message: "bad gateway"}, want: false},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			var (
				mu  sync.Mutex
				got []string
			)
			pool := providers.NewPool(models.ProviderWorkBuddy)
			pool.UpdateSlots(map[string]providers.Provider{
				models.ProviderWorkBuddy + ":cb-1": failingProvider{err: testCase.err},
			})
			pool.SetAuthRejectedHook(func(provider, accountID string) {
				mu.Lock()
				defer mu.Unlock()
				got = append(got, provider+"/"+accountID)
			})
			request := &chatwire.ChatRequest{Model: "glm-5.3"}
			if _, err := pool.Complete(context.Background(), request); err == nil {
				t.Fatal("want an error from the failing provider")
			}
			mu.Lock()
			reported := len(got) > 0
			mu.Unlock()
			if reported != testCase.want {
				t.Fatalf("hook fired = %v, want %v (calls: %v)", reported, testCase.want, got)
			}
			if testCase.want {
				mu.Lock()
				defer mu.Unlock()
				if got[0] != models.ProviderWorkBuddy+"/cb-1" {
					t.Fatalf("hook payload = %q, want provider/account", got[0])
				}
			}
		})
	}
}

// failingProvider always fails with the configured error, so the pool's failure
// path can be exercised without an upstream.
type failingProvider struct{ err error }

func (f failingProvider) Name() string { return "failing" }

func (f failingProvider) Complete(context.Context, *chatwire.ChatRequest) (map[string]any, error) {
	return nil, f.err
}

func (f failingProvider) Stream(context.Context, *chatwire.ChatRequest) (providers.FrameStream, error) {
	return nil, f.err
}

func (f failingProvider) Close() error { return nil }

// ageCredential backdates a credential row so the rolling trigger sees an old
// write. The scheduler reads updated_at, and nothing else in these tests may
// rewrite it.
func ageCredential(t *testing.T, db *store.DB, provider, accountID string, age time.Duration) {
	t.Helper()
	stamp := store.FormatISO(time.Now().UTC().Add(-age))
	if _, err := db.ExecContext(context.Background(),
		`UPDATE credentials SET updated_at=? WHERE provider=? AND account_id=?`, stamp, provider, accountID); err != nil {
		t.Fatalf("age credential: %v", err)
	}
}

// TestCredentialListReportsNextRefresh proves the console surface an operator
// uses to tell whether rotation is actually running. Without it, "updated_at is
// 5 hours old" reads as healthy at a 6-hour interval and as stalled at a 1-hour
// one, and there is no way to tell which.
func TestCredentialListReportsNextRefresh(t *testing.T) {
	api, db := newTestAPI(t)
	seedRotatableAccount(t, db, api.Vault, models.ProviderWorkBuddy, "cb-surface", "rt", "at")
	api.Settings.CredentialRefreshInterval = 6

	recorder := httptest.NewRecorder()
	api.handleListCredentials(recorder, httptest.NewRequest(http.MethodGet, "/api/admin/credentials", nil))
	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", recorder.Code, recorder.Body.String())
	}
	var body struct {
		IntervalHours int `json:"refresh_interval_hours"`
		Credentials   []struct {
			AccountID     string  `json:"account_id"`
			NextRefreshAt *string `json:"next_refresh_at"`
		} `json:"credentials"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.IntervalHours != 6 {
		t.Fatalf("refresh_interval_hours = %d, want 6", body.IntervalHours)
	}
	var found bool
	for _, credential := range body.Credentials {
		if credential.AccountID != "cb-surface" {
			continue
		}
		found = true
		if credential.NextRefreshAt == nil {
			t.Fatal("next_refresh_at must be present for a credential with a refresh token")
		}
		next, ok := store.ParseISO(*credential.NextRefreshAt)
		if !ok || !next.After(time.Now().UTC()) {
			t.Fatalf("next_refresh_at = %v, want a future instant", credential.NextRefreshAt)
		}
	}
	if !found {
		t.Fatal("the seeded credential must appear in the list")
	}
}
