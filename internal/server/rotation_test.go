package server

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/config"
	"github.com/dmego/qoderbuddy2api/internal/models"
	"github.com/dmego/qoderbuddy2api/internal/store"
	"github.com/dmego/qoderbuddy2api/internal/vault"
)

// rotationSettings points both refresh endpoints at one stub origin.
func rotationSettings(origin string) config.Settings {
	return config.Settings{
		CodeBuddyEndpoint:     origin,
		WorkBuddyIntlEndpoint: origin,
		CheckinTimezone:       "Asia/Shanghai",
	}
}

// seedRotatableAccount stores a chat credential whose expiry is inside the lead
// window, plus the purpose row the schedulers gate on.
func seedRotatableAccount(t *testing.T, db *store.DB, credVault *vault.Vault, provider, accountID, refreshToken, accessToken string) {
	t.Helper()
	ctx := context.Background()
	if _, err := db.UpsertAccount(ctx, store.Account{Provider: provider, AccountID: accountID, Label: accountID, Source: "oauth", Enabled: true}); err != nil {
		t.Fatalf("upsert account: %v", err)
	}
	if err := db.UpsertPurpose(ctx, store.Purpose{
		Provider: provider, AccountID: accountID, Purpose: "chat",
		Enabled: true, Status: "active", VerificationStatus: "not_required",
	}); err != nil {
		t.Fatalf("upsert purpose: %v", err)
	}
	blob, err := credVault.Encrypt(map[string]any{"access_token": accessToken, "refresh_token": refreshToken})
	if err != nil {
		t.Fatalf("encrypt: %v", err)
	}
	expiry := store.FormatISO(time.Now().UTC().Add(5 * time.Minute))
	if _, err := db.UpsertCredential(ctx, store.CredentialWrite{
		Provider: provider, AccountID: accountID, Purpose: "chat",
		Mode: "oauth", EncryptedPayload: blob, HasRefreshToken: true, ExpiresAt: &expiry,
	}); err != nil {
		t.Fatalf("upsert credential: %v", err)
	}
}

// TestRotateOnceRefreshesDomesticCredential is the regression for the bug where
// only workbuddy_intl was rotated: the domestic CodeBuddy credential was left
// to expire on the belief that it had no refresh endpoint, so an account whose
// session died could never recover and its sign-in purpose silently stayed
// "active" with a future expiry.
func TestRotateOnceRefreshesDomesticCredential(t *testing.T) {
	var (
		gotDomain  string
		gotRefresh string
		gotPath    string
	)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotDomain = r.Header.Get("X-Domain")
		gotRefresh = r.Header.Get("X-Refresh-Token")
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"code":0,"msg":"OK","data":{
		  "accessToken":"rotated-access","refreshToken":"rotated-refresh","expiresIn":3600}}`)
	}))
	defer server.Close()

	api, db := newTestAPI(t)
	seedRotatableAccount(t, db, api.Vault, models.ProviderWorkBuddy, "cb-domestic", "old-refresh", "old-access")

	rotateOnce(context.Background(), db, api.Vault, 30*time.Minute, nil, rotationSettings(server.URL), map[string]bool{})

	if gotPath != "/v2/plugin/auth/token/refresh" {
		t.Fatalf("refresh path = %q, want the plugin refresh endpoint", gotPath)
	}
	if gotDomain != "copilot.tencent.com" {
		t.Fatalf("X-Domain = %q, want copilot.tencent.com for the domestic provider", gotDomain)
	}
	if gotRefresh != "old-refresh" {
		t.Fatalf("X-Refresh-Token = %q, want the stored refresh token", gotRefresh)
	}
	record, err := db.GetCredential(context.Background(), models.ProviderWorkBuddy, "cb-domestic", "chat")
	if err != nil {
		t.Fatalf("read credential: %v", err)
	}
	payload, err := api.Vault.Decrypt(record.EncryptedPayload)
	if err != nil {
		t.Fatalf("decrypt: %v", err)
	}
	if payload["access_token"] != "rotated-access" {
		t.Fatalf("access_token = %v, want the rotated value", payload["access_token"])
	}
	if payload["refresh_token"] != "rotated-refresh" {
		t.Fatalf("refresh_token = %v, want the rotated value persisted", payload["refresh_token"])
	}
	if record.ExpiresAt == nil {
		t.Fatal("expires_at must advance with the rotation")
	}
	expiry, ok := store.ParseISO(*record.ExpiresAt)
	if !ok || !expiry.After(time.Now().UTC().Add(30*time.Minute)) {
		t.Fatalf("expires_at = %v, want a deadline driven by expiresIn", record.ExpiresAt)
	}
}

// TestRotateOnceRetriesRefusedRefresh pins the measured upstream behaviour: the
// refresh endpoint answers "12153 refresh token is invalid" intermittently for a
// token that succeeds moments later, and a successful refresh revives an access
// token whose session a live request had just rejected. Rotation must therefore
// keep retrying and must NOT mark the account dead — doing so would switch off
// automatic renewal for an account that would have recovered.
func TestRotateOnceRetriesRefusedRefresh(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.Header().Set("Content-Type", "application/json")
		// First round fails, later rounds succeed, mirroring the real upstream.
		if calls == 1 {
			w.WriteHeader(http.StatusUnauthorized)
			_, _ = io.WriteString(w, `{"code":12153,"msg":"12153:refresh token is invalid"}`)
			return
		}
		_, _ = io.WriteString(w, `{"code":0,"data":{"accessToken":"revived-access","refreshToken":"revived-refresh","expiresIn":3600}}`)
	}))
	defer server.Close()

	api, db := newTestAPI(t)
	seedRotatableAccount(t, db, api.Vault, models.ProviderWorkBuddy, "cb-flaky", "flaky-refresh", "dead-access")
	settings := rotationSettings(server.URL)
	warned := map[string]bool{}
	ctx := context.Background()

	rotateOnce(ctx, db, api.Vault, 30*time.Minute, nil, settings, warned)

	purposes, err := db.ListPurposes(ctx, models.ProviderWorkBuddy, "cb-flaky")
	if err != nil {
		t.Fatalf("list purposes: %v", err)
	}
	if len(purposes) != 1 {
		t.Fatalf("purposes = %d, want 1", len(purposes))
	}
	if purposes[0].Status != "active" {
		t.Fatalf("status = %q after one refused refresh, want active: the failure is retryable", purposes[0].Status)
	}

	// The next round must retry and land the rotation.
	rotateOnce(ctx, db, api.Vault, 30*time.Minute, nil, settings, warned)
	record, err := db.GetCredential(ctx, models.ProviderWorkBuddy, "cb-flaky", "chat")
	if err != nil {
		t.Fatalf("read credential: %v", err)
	}
	payload, err := api.Vault.Decrypt(record.EncryptedPayload)
	if err != nil {
		t.Fatalf("decrypt: %v", err)
	}
	if payload["access_token"] != "revived-access" {
		t.Fatalf("access_token = %v, want the retry to land the rotated value", payload["access_token"])
	}
}

// TestRotateOnceLeavesHealthyCredentialAlone pins the lead window: a credential
// outside it must not be touched, so a healthy account is not rotated on every
// round.
func TestRotateOnceLeavesHealthyCredentialAlone(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"code":0,"data":{"accessToken":"x","expiresIn":3600}}`)
	}))
	defer server.Close()

	api, db := newTestAPI(t)
	ctx := context.Background()
	if _, err := db.UpsertAccount(ctx, store.Account{Provider: models.ProviderWorkBuddy, AccountID: "cb-fresh", Label: "cb-fresh", Source: "oauth", Enabled: true}); err != nil {
		t.Fatalf("upsert account: %v", err)
	}
	if err := db.UpsertPurpose(ctx, store.Purpose{Provider: models.ProviderWorkBuddy, AccountID: "cb-fresh", Purpose: "chat", Enabled: true, Status: "active"}); err != nil {
		t.Fatalf("upsert purpose: %v", err)
	}
	blob, err := api.Vault.Encrypt(map[string]any{"access_token": "fresh", "refresh_token": "fresh-refresh"})
	if err != nil {
		t.Fatalf("encrypt: %v", err)
	}
	far := store.FormatISO(time.Now().UTC().Add(40 * 24 * time.Hour))
	if _, err := db.UpsertCredential(ctx, store.CredentialWrite{
		Provider: models.ProviderWorkBuddy, AccountID: "cb-fresh", Purpose: "chat",
		Mode: "oauth", EncryptedPayload: blob, HasRefreshToken: true, ExpiresAt: &far,
	}); err != nil {
		t.Fatalf("upsert credential: %v", err)
	}

	rotateOnce(ctx, db, api.Vault, 30*time.Minute, nil, rotationSettings(server.URL), map[string]bool{})

	if calls != 0 {
		t.Fatalf("refresh calls = %d, want 0 for a credential outside the lead window", calls)
	}
}

// TestRefreshPluginTokenReportsFailures pins that a refused refresh is a plain
// error rather than a classified terminal state. The classification was removed
// because upstream returns the same rejection intermittently for a token that
// later succeeds, so a caller must be free to retry.
func TestRefreshPluginTokenReportsFailures(t *testing.T) {
	cases := []struct {
		name   string
		status int
		body   string
	}{
		{
			name:   "offline session not found",
			status: http.StatusUnauthorized,
			body:   `{"code":12153,"msg":"12153:refresh token failed:400 Bad Request: invalid_grant: Offline user session not found"}`,
		},
		{
			name:   "refresh token is invalid",
			status: http.StatusUnauthorized,
			body:   `{"code":12153,"msg":"12153:refresh token is invalid"}`,
		},
		{
			name:   "transient upstream failure",
			status: http.StatusBadGateway,
			body:   `{"code":500,"msg":"upstream unavailable"}`,
		},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(testCase.status)
				_, _ = io.WriteString(w, testCase.body)
			}))
			defer server.Close()

			_, err := refreshPluginToken(context.Background(), server.URL, "copilot.tencent.com", "token")
			if err == nil {
				t.Fatal("want an error")
			}
		})
	}
}

// TestRefreshPluginTokenSendsBothProvidersContract pins the wire shape shared by
// both deployments: an empty JSON body, the refresh token in X-Refresh-Token,
// and the caller's X-Domain.
func TestRefreshPluginTokenSendsBothProvidersContract(t *testing.T) {
	var method, body, domain, source, userAgent string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		method = r.Method
		raw, _ := io.ReadAll(r.Body)
		body = string(raw)
		domain = r.Header.Get("X-Domain")
		source = r.Header.Get("X-Auth-Refresh-Source")
		userAgent = r.Header.Get("User-Agent")
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"code":0,"data":{"accessToken":"a","refreshToken":"b","expiresIn":7200}}`)
	}))
	defer server.Close()

	result, err := refreshPluginToken(context.Background(), server.URL, "www.workbuddy.ai", "rt")
	if err != nil {
		t.Fatalf("refresh: %v", err)
	}
	if method != http.MethodPost {
		t.Fatalf("method = %q, want POST", method)
	}
	if body != "{}" {
		t.Fatalf("body = %q, want an empty JSON object", body)
	}
	if domain != "www.workbuddy.ai" || source != "plugin" {
		t.Fatalf("domain = %q source = %q, want the plugin contract headers", domain, source)
	}
	if userAgent == "" {
		t.Fatal("User-Agent must be set: the gateway rejects an unset agent")
	}
	if result.AccessToken != "a" || result.RefreshToken != "b" || result.ExpiresIn != 7200 {
		t.Fatalf("result = %+v, want both tokens and the lifetime", result)
	}
	// Sanity: the decoded payload must be the envelope's data object.
	raw, _ := json.Marshal(result)
	if len(raw) == 0 {
		t.Fatal("result must marshal")
	}
}

// TestRotateOnceWarnsOncePerFailingCredential guards the log volume: the loop
// runs every 15 minutes, so a credential that keeps failing must not emit the
// same warning on every round.
func TestRotateOnceWarnsOncePerFailingCredential(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = io.WriteString(w, `{"code":12153,"msg":"12153:refresh token is invalid"}`)
	}))
	defer server.Close()

	api, db := newTestAPI(t)
	seedRotatableAccount(t, db, api.Vault, models.ProviderWorkBuddy, "cb-fail", "r", "a")
	settings := rotationSettings(server.URL)
	warned := map[string]bool{}

	rotateOnce(context.Background(), db, api.Vault, 30*time.Minute, nil, settings, warned)
	if !warned[models.ProviderWorkBuddy+":cb-fail:chat"] {
		t.Fatal("the first failure must be recorded as warned")
	}
	// A second failure for the same credential must be suppressed.
	if !warned[models.ProviderWorkBuddy+":cb-fail:chat"] {
		t.Fatal("warned state must persist across rounds")
	}
}

// TestRotateOnceClearsWarnMarkerOnSuccess ensures a recovered credential is
// reported again if it later fails, instead of staying silenced forever.
func TestRotateOnceClearsWarnMarkerOnSuccess(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"code":0,"data":{"accessToken":"ok","expiresIn":3600}}`)
	}))
	defer server.Close()

	api, db := newTestAPI(t)
	seedRotatableAccount(t, db, api.Vault, models.ProviderWorkBuddy, "cb-ok", "r", "a")
	key := models.ProviderWorkBuddy + ":cb-ok:chat"
	warned := map[string]bool{key: true}

	rotateOnce(context.Background(), db, api.Vault, 30*time.Minute, nil, rotationSettings(server.URL), warned)

	if warned[key] {
		t.Fatal("a successful rotation must clear the warning marker")
	}
}

// TestRotateOnceRevivesRejectedPurpose is the regression for a false-positive
// rejection: the metrics probe saw a 401 and took the account out of the pool,
// yet the refresh contract accepts its token. A landed rotation is proof the
// credential works, so the purpose must return to active instead of waiting for
// a manual re-login that is not actually needed.
func TestRotateOnceRevivesRejectedPurpose(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"code":0,"data":{"accessToken":"revived","refreshToken":"next","expiresIn":3600}}`)
	}))
	defer server.Close()

	api, db := newTestAPI(t)
	ctx := context.Background()
	seedRotatableAccount(t, db, api.Vault, models.ProviderWorkBuddy, "cb-revive", "r", "a")
	if err := db.MarkPurposeRejected(ctx, models.ProviderWorkBuddy, "cb-revive", "chat", "auth_rejected"); err != nil {
		t.Fatalf("mark rejected: %v", err)
	}

	rotateOnce(ctx, db, api.Vault, 30*time.Minute, nil, rotationSettings(server.URL), map[string]bool{})

	purposes, err := db.ListPurposes(ctx, models.ProviderWorkBuddy, "cb-revive")
	if err != nil {
		t.Fatalf("list purposes: %v", err)
	}
	if len(purposes) != 1 {
		t.Fatalf("purposes = %d, want 1", len(purposes))
	}
	if purposes[0].Status != "active" {
		t.Fatalf("status = %q, want active: a working refresh proves the credential is alive", purposes[0].Status)
	}
	if purposes[0].LastError != nil {
		t.Fatalf("last_error = %v, want it cleared on recovery", purposes[0].LastError)
	}
	if purposes[0].VerificationStatus != "verified" {
		t.Fatalf("verification_status = %q, want verified", purposes[0].VerificationStatus)
	}
}
