package server

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/checkin"
	"github.com/dmego/qoderbuddy2api/internal/importer"
	"github.com/dmego/qoderbuddy2api/internal/models"
	"github.com/dmego/qoderbuddy2api/internal/oauthflow"
	"github.com/dmego/qoderbuddy2api/internal/store"
)

// importAccount drives the real import writer, which is the component that
// decides which purpose rows a new account gets.
func importAccount(t *testing.T, api *API, provider, label string, payload map[string]any) string {
	t.Helper()
	writer := importer.New(api.DB, api.Vault)
	accountID, err := writer.UpsertImportedAccount(context.Background(), provider, "", label, nil, payload, "")
	if err != nil {
		t.Fatalf("import: %v", err)
	}
	return accountID
}

// mustEncrypt stores one credential payload through the API's own vault.
func mustEncrypt(t *testing.T, api *API, payload map[string]any) string {
	t.Helper()
	encrypted, err := api.Vault.Encrypt(payload)
	if err != nil {
		t.Fatalf("encrypt: %v", err)
	}
	return encrypted
}

// newTestCheckinService points a real sign-in service at a stub upstream.
func newTestCheckinService(t *testing.T, api *API, db *store.DB, baseURL string) *checkin.Service {
	t.Helper()
	client := checkin.NewClient(checkin.ClientOptions{
		BaseURL:     baseURL,
		StatusPath:  "/billing/meter/checkin-status",
		ClaimPath:   "/billing/meter/daily-checkin",
		ClaimMethod: http.MethodPost,
		Timeout:     5 * time.Second,
	})
	return checkin.NewService(checkin.ServiceOptions{
		DB:       db,
		Vault:    api.Vault,
		Registry: newAccountRegistry(db, api.Settings),
		Settings: api.Settings,
		Client:   client,
	})
}

// oauthServiceForTests builds the flow service the import routes use, wired to
// the given sign-in service so verification exercises the real client.
func oauthServiceForTests(t *testing.T, api *API, checkinService *checkin.Service) *oauthflow.Service {
	t.Helper()
	service := oauthflow.NewService(oauthflow.ServiceOptions{
		DB:       api.DB,
		Vault:    api.Vault,
		Settings: api.Settings,
		Store:    oauthflow.NewStore(15 * time.Minute),
		Imports:  importer.New(api.DB, api.Vault),
	})
	service.SetCheckinVerifier(func(ctx context.Context, accountID string, credential checkin.Credential) error {
		headers := checkin.Credential{Mode: credential.Mode, AccessToken: credential.AccessToken, Cookie: credential.Cookie}
		result, err := checkinService.RunCredential(ctx, accountID, headers)
		if err != nil {
			return err
		}
		if !result.OK() {
			return oauthflow.ErrCheckinRejected
		}
		return nil
	})
	return service
}

// errorCodeOf extracts the error code the console displays. The admin API
// reports failures as {"detail": "..."}, which is what the console surfaces.
func errorCodeOf(t *testing.T, recorder *httptest.ResponseRecorder) string {
	t.Helper()
	var body struct {
		Detail string `json:"detail"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode error body: %v (%s)", err, recorder.Body.String())
	}
	return body.Detail
}

// A freshly imported domestic account must carry the disabled check-in purpose
// row. The console renders the sign-in control only when the row exists, and the
// manual check-in import only updates an existing row — so without it the
// operator can never enable sign-in for that account.
func TestImportedDomesticAccountCarriesCheckinPurpose(t *testing.T) {
	api, db := newTestAPI(t)
	ctx := context.Background()

	imported := importAccount(t, api, models.ProviderWorkBuddy, "cb-test",
		map[string]any{"access_token": "token-value"})

	purposes, err := db.ListPurposes(ctx, models.ProviderWorkBuddy, imported)
	if err != nil {
		t.Fatalf("list purposes: %v", err)
	}
	byName := map[string]store.Purpose{}
	for _, purpose := range purposes {
		byName[purpose.Purpose] = purpose
	}
	checkinPurpose, ok := byName["checkin"]
	if !ok {
		t.Fatalf("imported account is missing the checkin purpose row: %#v", byName)
	}
	if checkinPurpose.Enabled {
		t.Fatal("the check-in purpose must start disabled until it is verified")
	}
	if checkinPurpose.Status != "unconfigured" || checkinPurpose.VerificationStatus != "unverified" {
		t.Fatalf("unexpected initial check-in state: %#v", checkinPurpose)
	}
}

// Re-importing must not reset a sign-in credential the operator already
// verified, or every re-login would silently disable the daily sign-in.
func TestReimportKeepsVerifiedCheckinPurpose(t *testing.T) {
	api, db := newTestAPI(t)
	ctx := context.Background()

	imported := importAccount(t, api, models.ProviderWorkBuddy, "cb-test",
		map[string]any{"access_token": "token-value"})
	now := store.NowISO()
	if err := db.UpsertPurpose(ctx, store.Purpose{
		Provider: models.ProviderWorkBuddy, AccountID: imported, Purpose: "checkin",
		Enabled: true, Status: "active", VerificationStatus: "verified", VerifiedAt: &now,
	}); err != nil {
		t.Fatalf("seed verified purpose: %v", err)
	}

	importAccount(t, api, models.ProviderWorkBuddy, "cb-test",
		map[string]any{"access_token": "token-value-2"})

	purposes, err := db.ListPurposes(ctx, models.ProviderWorkBuddy, imported)
	if err != nil {
		t.Fatalf("list purposes: %v", err)
	}
	for _, purpose := range purposes {
		if purpose.Purpose != "checkin" {
			continue
		}
		if !purpose.Enabled || purpose.VerificationStatus != "verified" {
			t.Fatalf("re-import must not reset the verified check-in purpose: %#v", purpose)
		}
		return
	}
	t.Fatal("the check-in purpose row disappeared on re-import")
}

// The international deployment has no sign-in centre, so it must not gain a
// check-in row it could never use.
func TestImportedInternationalAccountHasNoCheckinPurpose(t *testing.T) {
	api, db := newTestAPI(t)
	ctx := context.Background()

	imported := importAccount(t, api, models.ProviderWorkBuddyIntl, "wbintl-test",
		map[string]any{"access_token": "token-value"})

	purposes, err := db.ListPurposes(ctx, models.ProviderWorkBuddyIntl, imported)
	if err != nil {
		t.Fatalf("list purposes: %v", err)
	}
	for _, purpose := range purposes {
		if purpose.Purpose == "checkin" {
			t.Fatalf("the international deployment must stay chat-only: %#v", purpose)
		}
	}
}

// PATCH must honour the purposes block the console sends. Dropping it silently
// leaves the operator unable to enable sign-in for an account whose credential
// was imported by hand, because the API answers 200 while nothing changes.
func TestPatchAccountAppliesPurposeEnablement(t *testing.T) {
	api, db := newTestAPI(t)
	ctx := context.Background()

	imported := importAccount(t, api, models.ProviderWorkBuddy, "cb-test",
		map[string]any{"access_token": "token-value"})

	recorder := adminRequest(t, api, http.MethodPatch,
		"/api/admin/accounts/"+models.ProviderWorkBuddy+"/"+imported,
		map[string]any{"purposes": map[string]any{"checkin": map[string]any{"enabled": true}}})
	if recorder.Code != http.StatusOK {
		t.Fatalf("patch status = %d, body %s", recorder.Code, recorder.Body.String())
	}

	purposes, err := db.ListPurposes(ctx, models.ProviderWorkBuddy, imported)
	if err != nil {
		t.Fatalf("list purposes: %v", err)
	}
	for _, purpose := range purposes {
		if purpose.Purpose != "checkin" {
			continue
		}
		if !purpose.Enabled {
			t.Fatal("the check-in purpose was not enabled by the patch")
		}
		return
	}
	t.Fatal("no check-in purpose row to patch")
}

// The account detail view is what the console binds its check-in checkbox to.
func TestAccountViewExposesCheckinPurpose(t *testing.T) {
	api, _ := newTestAPI(t)

	imported := importAccount(t, api, models.ProviderWorkBuddy, "cb-test",
		map[string]any{"access_token": "token-value"})

	recorder := adminRequest(t, api, http.MethodGet,
		"/api/admin/accounts/"+models.ProviderWorkBuddy+"/"+imported, nil)
	if recorder.Code != http.StatusOK {
		t.Fatalf("get status = %d, body %s", recorder.Code, recorder.Body.String())
	}
	var view struct {
		Purposes map[string]map[string]any `json:"purposes"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &view); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if _, ok := view.Purposes["checkin"]; !ok {
		t.Fatalf("the account view omits the check-in purpose: %#v", view.Purposes)
	}
}

// A verify-checkin request must actually run the sign-in for the account
// instead of only re-reading the stored daily state: the console offers the
// button precisely to turn an unverified credential into a verified one.
func TestVerifyCheckinRunsTheSignIn(t *testing.T) {
	api, db := newTestAPI(t)
	ctx := context.Background()

	imported := importAccount(t, api, models.ProviderWorkBuddy, "cb-test",
		map[string]any{"access_token": "token-value"})
	if _, err := db.UpsertCredential(ctx, store.CredentialWrite{
		Provider:         models.ProviderWorkBuddy,
		AccountID:        imported,
		Purpose:          "checkin",
		Mode:             "bearer",
		EncryptedPayload: mustEncrypt(t, api, map[string]any{"access_token": "token-value"}),
	}); err != nil {
		t.Fatalf("upsert credential: %v", err)
	}

	var calls int
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"code":0,"data":{"status":"CLAIMED"}}`))
	}))
	defer upstream.Close()
	api.Checkin = newTestCheckinService(t, api, db, upstream.URL)

	recorder := adminRequest(t, api, http.MethodPost,
		"/api/admin/accounts/"+models.ProviderWorkBuddy+"/"+imported+"/verify-checkin", nil)
	if recorder.Code != http.StatusOK {
		t.Fatalf("verify status = %d, body %s", recorder.Code, recorder.Body.String())
	}
	if calls == 0 {
		t.Fatal("verify-checkin did not call the upstream sign-in endpoint")
	}
	var body struct {
		Results []map[string]any `json:"results"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(body.Results) != 1 {
		t.Fatalf("verify-checkin must report one result, got %#v", body.Results)
	}
	if outcome, _ := body.Results[0]["outcome"].(string); outcome != "claimed" {
		t.Fatalf("unexpected verify outcome: %#v", body.Results[0])
	}
}

// After a successful sign-in the purpose must read as verified and enabled, so
// the console stops offering the manual import as the only way forward.
func TestSuccessfulCheckinVerifiesThePurpose(t *testing.T) {
	api, db := newTestAPI(t)
	ctx := context.Background()

	imported := importAccount(t, api, models.ProviderWorkBuddy, "cb-test",
		map[string]any{"access_token": "token-value"})
	if _, err := db.UpsertCredential(ctx, store.CredentialWrite{
		Provider:         models.ProviderWorkBuddy,
		AccountID:        imported,
		Purpose:          "checkin",
		Mode:             "bearer",
		EncryptedPayload: mustEncrypt(t, api, map[string]any{"access_token": "token-value"}),
	}); err != nil {
		t.Fatalf("upsert credential: %v", err)
	}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"code":0,"data":{"status":"CLAIMED"}}`))
	}))
	defer upstream.Close()
	api.Checkin = newTestCheckinService(t, api, db, upstream.URL)

	recorder := adminRequest(t, api, http.MethodPost,
		"/api/admin/accounts/"+models.ProviderWorkBuddy+"/"+imported+"/verify-checkin", nil)
	if recorder.Code != http.StatusOK {
		t.Fatalf("verify status = %d, body %s", recorder.Code, recorder.Body.String())
	}

	purposes, err := db.ListPurposes(ctx, models.ProviderWorkBuddy, imported)
	if err != nil {
		t.Fatalf("list purposes: %v", err)
	}
	for _, purpose := range purposes {
		if purpose.Purpose != "checkin" {
			continue
		}
		if !purpose.Enabled || purpose.Status != "active" || purpose.VerificationStatus != "verified" {
			t.Fatalf("a successful sign-in must activate the purpose: %#v", purpose)
		}
		return
	}
	t.Fatal("no check-in purpose row to verify")
}

// A rejected sign-in credential must be reported with the code the console
// explains, and nothing may be stored: an unverified token in the database would
// be indistinguishable from a working one.
func TestManualCheckinImportRejectsBadCredential(t *testing.T) {
	api, db := newTestAPI(t)
	ctx := context.Background()

	imported := importAccount(t, api, models.ProviderWorkBuddy, "cb-test",
		map[string]any{"access_token": "token-value"})

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"code":40001,"msg":"invalid token"}`))
	}))
	defer upstream.Close()
	api.Imports = oauthServiceForTests(t, api, newTestCheckinService(t, api, db, upstream.URL))

	recorder := adminRequest(t, api, http.MethodPost,
		"/api/admin/auth/codebuddy/checkin",
		map[string]any{"account_id": imported, "mode": "bearer", "access_token": "bad-token"})
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400: %s", recorder.Code, recorder.Body.String())
	}
	if code := errorCodeOf(t, recorder); code != "checkin_credential_rejected" {
		t.Fatalf("error code = %q, want checkin_credential_rejected", code)
	}
	if _, err := db.GetCredential(ctx, models.ProviderWorkBuddy, imported, "checkin"); err == nil {
		t.Fatal("a rejected credential must not be stored")
	}
}

// The manual sign-in import is the path that turns an unconfigured account into
// a signing-in one, so it must leave the purpose enabled and verified.
func TestManualCheckinImportVerifiesAndEnablesPurpose(t *testing.T) {
	api, db := newTestAPI(t)
	ctx := context.Background()

	imported := importAccount(t, api, models.ProviderWorkBuddy, "cb-test",
		map[string]any{"access_token": "token-value"})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"code":0,"data":{"status":"CLAIMED"}}`))
	}))
	defer upstream.Close()
	api.Imports = oauthServiceForTests(t, api, newTestCheckinService(t, api, db, upstream.URL))

	recorder := adminRequest(t, api, http.MethodPost,
		"/api/admin/auth/codebuddy/checkin",
		map[string]any{"account_id": imported, "mode": "bearer", "access_token": "good-token"})
	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, body %s", recorder.Code, recorder.Body.String())
	}

	purposes, err := db.ListPurposes(ctx, models.ProviderWorkBuddy, imported)
	if err != nil {
		t.Fatalf("list purposes: %v", err)
	}
	for _, purpose := range purposes {
		if purpose.Purpose != "checkin" {
			continue
		}
		if !purpose.Enabled || purpose.VerificationStatus != "verified" || purpose.Status != "active" {
			t.Fatalf("the verified sign-in credential must activate the purpose: %#v", purpose)
		}
		return
	}
	t.Fatal("no check-in purpose row after the manual import")
}

// The console revokes and rotates keys with POST .../{id}/{action}; a route
// registered only as DELETE answers 404 and the operator cannot revoke a leaked
// key from the console.
func TestProxyKeyRevokeAndRotateRoutes(t *testing.T) {
	api, db := newTestAPI(t)
	ctx := context.Background()

	created := adminRequest(t, api, http.MethodPost, "/api/admin/proxy-keys",
		map[string]any{"name": "test-key", "scopes": []string{"proxy"}})
	if created.Code != http.StatusCreated {
		t.Fatalf("create status = %d, body %s", created.Code, created.Body.String())
	}
	var key struct {
		KeyID string `json:"key_id"`
		Key   string `json:"key"`
	}
	if err := json.Unmarshal(created.Body.Bytes(), &key); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if key.Key == "" || key.KeyID == "" {
		t.Fatalf("the raw key must be revealed once: %#v", key)
	}

	revoked := adminRequest(t, api, http.MethodPost,
		"/api/admin/proxy-keys/"+key.KeyID+"/revoke", nil)
	if revoked.Code != http.StatusOK {
		t.Fatalf("revoke status = %d, body %s", revoked.Code, revoked.Body.String())
	}
	keys, err := db.ListProxyKeys(ctx)
	if err != nil {
		t.Fatalf("list keys: %v", err)
	}
	// Deletion must remove the row outright: a soft-revoked key would linger in
	// the list as 已撤销 and read like the delete never happened.
	for _, item := range keys {
		if item.KeyID == key.KeyID {
			t.Fatalf("the deleted key must be gone from the list, got %#v", item)
		}
	}

	// Rotating a key that no longer exists must fail rather than mint a live key
	// from a dead one.
	rotated := adminRequest(t, api, http.MethodPost,
		"/api/admin/proxy-keys/"+key.KeyID+"/rotate", nil)
	if rotated.Code != http.StatusNotFound {
		t.Fatalf("rotating a deleted key returned %d, want 404: %s", rotated.Code, rotated.Body.String())
	}

	// A live key rotates: the old row is deleted and a revealed replacement
	// takes its place.
	fresh := adminRequest(t, api, http.MethodPost, "/api/admin/proxy-keys",
		map[string]any{"name": "rotatable", "scopes": []string{"proxy"}})
	var freshKey struct {
		KeyID string `json:"key_id"`
	}
	if err := json.Unmarshal(fresh.Body.Bytes(), &freshKey); err != nil {
		t.Fatalf("decode: %v", err)
	}
	rotation := adminRequest(t, api, http.MethodPost,
		"/api/admin/proxy-keys/"+freshKey.KeyID+"/rotate", nil)
	if rotation.Code != http.StatusCreated {
		t.Fatalf("rotate status = %d, body %s", rotation.Code, rotation.Body.String())
	}
	var replacement struct {
		KeyID         string `json:"key_id"`
		Key           string `json:"key"`
		ReplacedKeyID string `json:"replaced_key_id"`
	}
	if err := json.Unmarshal(rotation.Body.Bytes(), &replacement); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if replacement.Key == "" {
		t.Fatal("rotation must reveal the replacement secret")
	}
	if replacement.ReplacedKeyID != freshKey.KeyID {
		t.Fatalf("replaced_key_id = %q, want %q", replacement.ReplacedKeyID, freshKey.KeyID)
	}
	keys, err = db.ListProxyKeys(ctx)
	if err != nil {
		t.Fatalf("list keys: %v", err)
	}
	for _, item := range keys {
		if item.KeyID == freshKey.KeyID {
			t.Fatalf("the rotated-away key must be deleted, got %#v", item)
		}
		if item.KeyID == replacement.KeyID && item.Name != "rotatable" {
			t.Fatalf("the replacement must inherit the name: %#v", item)
		}
	}
}

// Legacy rows that were soft-revoked by earlier versions must still be
// deletable, so residue can be cleaned up from the console.
func TestProxyKeyRevokeCleansUpLegacyRevokedRow(t *testing.T) {
	api, db := newTestAPI(t)
	ctx := context.Background()

	legacy := store.ProxyKey{
		KeyID: "pk_legacy", Name: "legacy", KeyHash: hashToken("legacy-raw"),
		Scopes: []string{"proxy"}, Enabled: true, CreatedAt: store.NowISO(),
	}
	if err := db.CreateProxyKey(ctx, legacy); err != nil {
		t.Fatalf("seed: %v", err)
	}
	revokedAt := store.NowISO()
	if err := db.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.Exec("UPDATE proxy_api_keys SET revoked_at=? WHERE key_id=?", revokedAt, legacy.KeyID)
		return err
	}); err != nil {
		t.Fatalf("soft revoke: %v", err)
	}

	recorder := adminRequest(t, api, http.MethodDelete,
		"/api/admin/proxy-keys/"+legacy.KeyID, nil)
	if recorder.Code != http.StatusOK {
		t.Fatalf("delete status = %d, body %s", recorder.Code, recorder.Body.String())
	}
	keys, err := db.ListProxyKeys(ctx)
	if err != nil {
		t.Fatalf("list keys: %v", err)
	}
	for _, item := range keys {
		if item.KeyID == legacy.KeyID {
			t.Fatalf("the legacy revoked row must be deletable, got %#v", item)
		}
	}
}

// A missing key must be reported as not found rather than silently "revoked".
func TestProxyKeyRevokeUnknownKeyIs404(t *testing.T) {
	api, _ := newTestAPI(t)

	recorder := adminRequest(t, api, http.MethodPost,
		"/api/admin/proxy-keys/pk_missing/revoke", nil)
	if recorder.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404: %s", recorder.Code, recorder.Body.String())
	}
}
