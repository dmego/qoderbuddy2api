package oauthflow

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"path/filepath"
	"testing"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/checkin"
	"github.com/dmego/qoderbuddy2api/internal/importer"
	"github.com/dmego/qoderbuddy2api/internal/models"
	"github.com/dmego/qoderbuddy2api/internal/store"
	"github.com/dmego/qoderbuddy2api/internal/vault"
)

const testVaultKey = "3J8Yq0mMx7v1pQ2wR4tY6uI8oP0aS2dF4gH6jK8lM9n="

// recordingWriter captures what the flow service hands to storage.
type recordingWriter struct {
	calls []struct {
		Provider     string
		AccountID    string
		Label        string
		IdentityHash *string
		Payload      map[string]any
		ExpiresAt    string
	}
}

func (w *recordingWriter) UpsertImportedAccount(
	_ context.Context,
	provider, accountID, label string,
	identityHash *string,
	payload map[string]any,
	expiresAt string,
) (string, error) {
	w.calls = append(w.calls, struct {
		Provider     string
		AccountID    string
		Label        string
		IdentityHash *string
		Payload      map[string]any
		ExpiresAt    string
	}{provider, accountID, label, identityHash, payload, expiresAt})
	if accountID == "" {
		accountID = "generated-id"
	}
	return accountID, nil
}

// A flow past its deadline must not be completable: an abandoned login that the
// operator finished hours later must not silently import a credential.
func TestExpiredFlowIsNotResolvable(t *testing.T) {
	flowStore := NewStore(15 * time.Minute)
	flow := Flow{
		FlowID:    "flow-1",
		Provider:  "codebuddy",
		CreatedAt: store.FormatISO(time.Now().UTC().Add(-2 * time.Hour)),
		ExpiresAt: store.FormatISO(time.Now().UTC().Add(-time.Minute)),
		Status:    "pending",
	}
	if err := flowStore.Put(flow); err != nil {
		t.Fatalf("Put: %v", err)
	}
	got, err := flowStore.Get("flow-1")
	if err != nil {
		t.Fatalf("Get on an expired flow must report its state, got error %v", err)
	}
	if got.Status != "expired" {
		t.Fatalf("expected status expired, got %q", got.Status)
	}
	// It must also be gone, so a second poll cannot resurrect it.
	if _, err := flowStore.Get("flow-1"); err != ErrNotFound {
		t.Fatalf("expired flow must be dropped, got err=%v", err)
	}
}

func TestLiveFlowRoundTripsAndCompletes(t *testing.T) {
	flowStore := NewStore(15 * time.Minute)
	flow := Flow{
		FlowID:    "flow-2",
		Provider:  "workbuddy_intl",
		CreatedAt: store.FormatISO(time.Now().UTC()),
		ExpiresAt: store.FormatISO(time.Now().UTC().Add(10 * time.Minute)),
		Status:    "pending",
	}
	if err := flowStore.Put(flow); err != nil {
		t.Fatalf("Put: %v", err)
	}
	got, err := flowStore.Get("flow-2")
	if err != nil || got.Status != "pending" {
		t.Fatalf("live flow must be pending, got %#v err=%v", got, err)
	}
	if err := flowStore.Complete("flow-2", "success", "wbintl-1", ""); err != nil {
		t.Fatalf("Complete: %v", err)
	}
	done, _ := flowStore.Get("flow-2")
	if done.Status != "success" || done.AccountID != "wbintl-1" {
		t.Fatalf("completion not recorded: %#v", done)
	}
	if err := flowStore.Complete("missing", "success", "", ""); err != ErrNotFound {
		t.Fatalf("completing an unknown flow must fail, got %v", err)
	}
}

// The manual import path must not depend on the network, and the credential it
// stores must be encrypted: a plaintext bearer token in the credentials table is
// the single worst outcome this package can produce.
func TestManualImportStoresEncryptedCredential(t *testing.T) {
	dir := t.TempDir()
	db, err := store.Open(filepath.Join(dir, "qb2api.sqlite3"))
	if err != nil {
		t.Fatalf("Open db: %v", err)
	}
	defer db.Close()
	credVault, err := vault.New(testVaultKey)
	if err != nil {
		t.Fatalf("vault: %v", err)
	}
	writer := &recordingWriter{}
	service := NewService(ServiceOptions{
		DB:       db,
		Vault:    credVault,
		Settings: testSettings(),
		Store:    NewStore(15 * time.Minute),
		Imports:  writer,
	})

	const secret = "intl-bearer-token-value"
	flow, err := service.Manual(context.Background(), "workbuddy_intl", "", "user@example.com",
		map[string]any{"access_token": secret, "refresh_token": "refresh-value"}, "")
	if err != nil {
		t.Fatalf("Manual: %v", err)
	}
	if flow.Status != "success" || flow.AccountID == "" {
		t.Fatalf("manual import must complete with an account id: %#v", flow)
	}
	if len(writer.calls) != 1 {
		t.Fatalf("expected one storage call, got %d", len(writer.calls))
	}
	call := writer.calls[0]
	if call.Payload["access_token"] != secret {
		t.Fatal("the writer must receive the real token")
	}
	if call.Label != "user@example.com" {
		t.Fatalf("the operator's label must be kept, got %q", call.Label)
	}
	// This token is opaque, so it names no upstream account: the import must
	// carry no identity at all rather than hash the operator's label, which is
	// page-level text and not an identity.
	if call.IdentityHash != nil {
		t.Fatalf("an opaque token must not produce an identity, got %q", *call.IdentityHash)
	}
}

// A token that carries the upstream account is de-duplicated by that account,
// never by the display label: the label is what the console collected, and two
// logins of the same WorkBuddy account can be named differently.
func TestImportIdentityComesFromTheTokenNotTheLabel(t *testing.T) {
	credVault, err := vault.New(testVaultKey)
	if err != nil {
		t.Fatalf("vault: %v", err)
	}
	const sub = "f75ce9c9-da0c-45c7-afb0-12fbc2baa497"
	token := testToken(map[string]any{"sub": sub, "preferred_username": "13126806311"})

	for _, input := range []struct{ label, wantLabel string }{
		{"", "13126806311"},
		{"主账号", "主账号"},
	} {
		writer := &recordingWriter{}
		service := NewService(ServiceOptions{
			Vault: credVault, Settings: testSettings(),
			Store: NewStore(time.Minute), Imports: writer,
		})
		if _, err := service.Manual(context.Background(), "codebuddy", "", input.label,
			map[string]any{"access_token": token}, ""); err != nil {
			t.Fatalf("Manual(%q): %v", input.label, err)
		}
		call := writer.calls[0]
		if call.IdentityHash == nil {
			t.Fatalf("label %q: the token's account must be the identity", input.label)
		}
		want := credVault.Fingerprint(sub)
		if *call.IdentityHash != want {
			t.Fatalf("label %q: identity hash %q, want the fingerprint of the token's sub", input.label, *call.IdentityHash)
		}
		if call.Label != input.wantLabel {
			t.Fatalf("label %q: stored %q, want %q", input.label, call.Label, input.wantLabel)
		}
	}
}

// Two browser logins that name no upstream account must land as two accounts.
//
// The de-duplication identity used to be the display label the console
// collected. An unlabelled login posts the same default label every time, so
// both logins hashed to the same identity: the second one overwrote the first
// account's credential and the console never showed a new user.
func TestUnlabelledLoginsImportSeparateAccounts(t *testing.T) {
	dir := t.TempDir()
	db, err := store.Open(filepath.Join(dir, "qb2api.sqlite3"))
	if err != nil {
		t.Fatalf("Open db: %v", err)
	}
	defer db.Close()
	credVault, err := vault.New(testVaultKey)
	if err != nil {
		t.Fatalf("vault: %v", err)
	}
	service := NewService(ServiceOptions{
		DB:       db,
		Vault:    credVault,
		Settings: testSettings(),
		Store:    NewStore(15 * time.Minute),
		Imports:  importer.New(db, credVault),
	})
	ctx := context.Background()

	firstToken := testToken(map[string]any{"sub": "f75ce9c9-da0c-45c7-afb0-12fbc2baa497"})
	secondToken := testToken(map[string]any{"sub": "0040c1cd-42b4-48e1-a385-85e2d7067206"})

	first, err := service.Manual(ctx, "codebuddy", "", "", map[string]any{"access_token": firstToken}, "")
	if err != nil {
		t.Fatalf("first login: %v", err)
	}
	second, err := service.Manual(ctx, "codebuddy", "", "", map[string]any{"access_token": secondToken}, "")
	if err != nil {
		t.Fatalf("second login: %v", err)
	}
	if first.AccountID == second.AccountID {
		t.Fatalf("both logins imported into %q: a new login must add an account", first.AccountID)
	}
	accounts, err := db.ListAccounts(ctx, models.KnownProviders)
	if err != nil {
		t.Fatalf("list accounts: %v", err)
	}
	if len(accounts) != 2 {
		t.Fatalf("expected 2 imported accounts, got %d", len(accounts))
	}
	// The first login's credential must be untouched by the second.
	firstCredential, err := db.GetCredential(ctx, "codebuddy", first.AccountID, "chat")
	if err != nil {
		t.Fatalf("first credential: %v", err)
	}
	if firstCredential.CredentialVersion != 1 {
		t.Fatalf("the first account's credential was rewritten (version %d)", firstCredential.CredentialVersion)
	}
	// A repeat login of the first account refreshes it instead of adding a slot.
	again, err := service.Manual(ctx, "codebuddy", "", "", map[string]any{"access_token": firstToken}, "")
	if err != nil {
		t.Fatalf("repeat login: %v", err)
	}
	if again.AccountID != first.AccountID {
		t.Fatalf("a re-login created %q instead of refreshing %q", again.AccountID, first.AccountID)
	}
	if len(listAccountsOrFail(t, db)) != 2 {
		t.Fatal("a re-login must not add an account")
	}
}

func listAccountsOrFail(t *testing.T, db *store.DB) []store.Account {
	t.Helper()
	accounts, err := db.ListAccounts(context.Background(), models.KnownProviders)
	if err != nil {
		t.Fatalf("list accounts: %v", err)
	}
	return accounts
}

// The re-auth entry point sends the account the operator chose; that credential
// must land on that account rather than create another slot.
//
// The console's account detail page passes account_id, and the domestic
// deployment's token names no account, so without it a re-login for an existing
// account would add a duplicate and leave the original broken.
func TestReauthorizationLandsOnTheRequestedAccount(t *testing.T) {
	dir := t.TempDir()
	db, err := store.Open(filepath.Join(dir, "qb2api.sqlite3"))
	if err != nil {
		t.Fatalf("Open db: %v", err)
	}
	defer db.Close()
	credVault, err := vault.New(testVaultKey)
	if err != nil {
		t.Fatalf("vault: %v", err)
	}
	service := NewService(ServiceOptions{
		DB: db, Vault: credVault, Settings: testSettings(),
		Store: NewStore(15 * time.Minute), Imports: importer.New(db, credVault),
	})
	ctx := context.Background()
	if _, err := db.UpsertAccount(ctx, store.Account{
		Provider: "codebuddy", AccountID: "cb-existing", Label: "main", Source: "oauth", Enabled: true,
	}); err != nil {
		t.Fatalf("seed account: %v", err)
	}

	flow, err := service.Manual(ctx, "codebuddy", "cb-existing", "main",
		map[string]any{"access_token": "opaque-token"}, "")
	if err != nil {
		t.Fatalf("re-authorization: %v", err)
	}
	if flow.AccountID != "cb-existing" {
		t.Fatalf("re-authorization imported into %q instead of cb-existing", flow.AccountID)
	}
	if count := len(listAccountsOrFail(t, db)); count != 1 {
		t.Fatalf("re-authorization must not add an account, got %d", count)
	}
	if _, err := db.GetCredential(ctx, "codebuddy", "cb-existing", "chat"); err != nil {
		t.Fatalf("the requested account has no chat credential: %v", err)
	}
}

// testToken renders an unsigned JWT carrying the given claims, which is all the
// identity reader looks at.
func testToken(claims map[string]any) string {
	payload, err := json.Marshal(claims)
	if err != nil {
		panic(err)
	}
	return "header." + base64.RawURLEncoding.EncodeToString(payload) + ".signature"
}

// The check-in import writes the credential itself, so assert on the stored row:
// it must decrypt and must not contain the plaintext.
func TestManualCheckinStoresEncryptedCredential(t *testing.T) {
	dir := t.TempDir()
	db, err := store.Open(filepath.Join(dir, "qb2api.sqlite3"))
	if err != nil {
		t.Fatalf("Open db: %v", err)
	}
	defer db.Close()
	credVault, err := vault.New(testVaultKey)
	if err != nil {
		t.Fatalf("vault: %v", err)
	}
	service := NewService(ServiceOptions{
		DB:       db,
		Vault:    credVault,
		Settings: testSettings(),
		Store:    NewStore(15 * time.Minute),
		Imports:  &recordingWriter{},
	})
	// The import is gated on a successful upstream sign-in, so the test must
	// supply the verifier the server wires in production.
	service.SetCheckinVerifier(func(context.Context, string, checkin.Credential) error { return nil })
	ctx := context.Background()
	if _, err := db.UpsertAccount(ctx, store.Account{
		Provider: "codebuddy", AccountID: "cb-1", Label: "main", Source: "manual", Enabled: true,
	}); err != nil {
		t.Fatalf("seed account: %v", err)
	}

	const cookie = "session=super-secret-cookie"
	if _, err := service.ManualCheckin(ctx, "codebuddy", "cb-1", "cookie",
		map[string]any{"cookie": cookie}, ""); err != nil {
		t.Fatalf("ManualCheckin: %v", err)
	}

	record, err := db.GetCredential(ctx, "codebuddy", "cb-1", "checkin")
	if err != nil {
		t.Fatalf("stored checkin credential not found: %v", err)
	}
	if record.EncryptedPayload == cookie {
		t.Fatal("credential stored in plaintext")
	}
	payload, err := credVault.Decrypt(record.EncryptedPayload)
	if err != nil {
		t.Fatalf("stored credential does not decrypt: %v", err)
	}
	if payload["cookie"] != cookie {
		t.Fatalf("decrypted payload lost the cookie: %#v", payloadKeys(payload))
	}
	if record.Mode != "cookie" {
		t.Fatalf("expected mode cookie, got %q", record.Mode)
	}
}

// The international deployment has no sign-in centre, so a check-in import for
// it must be refused rather than silently storing an unusable credential.
func TestManualCheckinRejectsInternationalProvider(t *testing.T) {
	dir := t.TempDir()
	db, err := store.Open(filepath.Join(dir, "qb2api.sqlite3"))
	if err != nil {
		t.Fatalf("Open db: %v", err)
	}
	defer db.Close()
	credVault, _ := vault.New(testVaultKey)
	service := NewService(ServiceOptions{
		DB: db, Vault: credVault, Settings: testSettings(),
		Store: NewStore(time.Minute), Imports: &recordingWriter{},
	})
	_, err = service.ManualCheckin(context.Background(), "workbuddy_intl", "wbintl-1", "bearer",
		map[string]any{"access_token": "x"}, "")
	if err != ErrUnsupportedProvider {
		t.Fatalf("expected ErrUnsupportedProvider, got %v", err)
	}
}

func TestManualImportRequiresAToken(t *testing.T) {
	credVault, _ := vault.New(testVaultKey)
	service := NewService(ServiceOptions{
		Vault: credVault, Settings: testSettings(),
		Store: NewStore(time.Minute), Imports: &recordingWriter{},
	})
	if _, err := service.Manual(context.Background(), "codebuddy", "", "x", map[string]any{}, ""); err == nil {
		t.Fatal("an empty payload must be rejected")
	}
	if _, err := service.Manual(context.Background(), "qoder", "", "x",
		map[string]any{"access_token": "t"}, ""); err != ErrUnsupportedProvider {
		t.Fatalf("a dropped provider must be rejected, got %v", err)
	}
}

// parsePoll must read the provider's envelope, including the pending marker that
// keeps the console polling.
func TestParsePollEnvelope(t *testing.T) {
	pending := parsePoll([]byte(`{"code":11217,"msg":"waiting"}`))
	if pending.Status != "pending" {
		t.Fatalf("expected pending, got %#v", pending)
	}
	success := parsePoll([]byte(`{"code":0,"data":{"accessToken":"a","refreshToken":"r","expiresIn":3600}}`))
	if success.Status != "success" || success.AccessToken != "a" || success.RefreshToken != "r" || success.ExpiresIn != 3600 {
		t.Fatalf("snake/camel accessors not handled: %#v", success)
	}
	snake := parsePoll([]byte(`{"code":0,"data":{"access_token":"a2","expires_in":"7200"}}`))
	if snake.AccessToken != "a2" || snake.ExpiresIn != 7200 {
		t.Fatalf("snake_case accessors not handled: %#v", snake)
	}
	noToken := parsePoll([]byte(`{"code":0,"data":{}}`))
	if noToken.Status != "error" || noToken.Message != "auth_poll_no_token" {
		t.Fatalf("a tokenless success must be an error: %#v", noToken)
	}
	broken := parsePoll([]byte(`not json`))
	if broken.Status != "error" || broken.Message != "auth_poll_invalid_json" {
		t.Fatalf("invalid json must be an error: %#v", broken)
	}
}

func payloadKeys(payload map[string]any) []string {
	out := make([]string, 0, len(payload))
	for key := range payload {
		out = append(out, key)
	}
	return out
}
