package server

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/dmego/qoderbuddy2api/gobackend/internal/models"
	"github.com/dmego/qoderbuddy2api/gobackend/internal/store"
	"github.com/dmego/qoderbuddy2api/gobackend/internal/vault"
)

// rotateOnce refreshes every credential whose expiry is inside the lead window.
//
// Only `workbuddy_intl` has a refresh contract in the kept provider set. The
// domestic WorkBuddy/CodeBuddy chat credential has NO documented refresh
// endpoint (the Python build never called one for it), so an expiring domestic
// credential is reported and left alone rather than being probed with a
// guessed request.
func rotateOnce(ctx context.Context, db *store.DB, credVault *vault.Vault, lead time.Duration, plane *ProxyPlane) {
	roundCtx, cancel := context.WithTimeout(ctx, 2*time.Minute)
	defer cancel()
	credentials, err := db.ListCredentials(roundCtx, models.KnownProviders)
	if err != nil {
		slog.Warn("credential rotation lookup failed", "error", err)
		return
	}
	now := time.Now().UTC()
	for _, credential := range credentials {
		if credential.ExpiresAt == nil {
			continue
		}
		expiresAt, ok := store.ParseISO(*credential.ExpiresAt)
		if !ok {
			continue
		}
		if expiresAt.Sub(now) > lead {
			continue
		}
		if credential.Provider != models.ProviderWorkBuddyIntl {
			// Nothing to do without a refresh contract; surface it so the
			// operator can re-import before the credential actually lapses.
			if expiresAt.Before(now) {
				slog.Warn("credential expired without a refresh contract",
					"provider", credential.Provider, "account", credential.AccountID,
					"purpose", credential.Purpose)
			}
			continue
		}
		if rotateIntl(roundCtx, db, credVault, credential, plane) {
			slog.Info("rotated credential",
				"provider", credential.Provider, "account", credential.AccountID,
				"purpose", credential.Purpose)
		}
	}
}

// rotateIntl refreshes one international credential and persists the new pair.
// It reports whether a rotation actually landed.
func rotateIntl(ctx context.Context, db *store.DB, credVault *vault.Vault, credential store.Credential, plane *ProxyPlane) bool {
	record, err := db.GetCredential(ctx, credential.Provider, credential.AccountID, credential.Purpose)
	if err != nil {
		slog.Warn("rotation skipped: credential row unreadable", "account", credential.AccountID, "error", err)
		return false
	}
	payload, err := credVault.Decrypt(record.EncryptedPayload)
	if err != nil {
		slog.Warn("rotation skipped: credential undecryptable", "account", credential.AccountID, "error", err)
		return false
	}
	refreshToken, _ := payload["refresh_token"].(string)
	if refreshToken == "" {
		slog.Warn("rotation skipped: no refresh token", "account", credential.AccountID)
		return false
	}
	result, err := refreshIntlToken(ctx, refreshToken)
	if err != nil {
		slog.Warn("intl token refresh failed", "account", credential.AccountID, "error", err)
		return false
	}
	if result.AccessToken == "" {
		slog.Warn("intl token refresh returned no access token", "account", credential.AccountID)
		return false
	}
	updated := map[string]any{}
	for key, value := range payload {
		updated[key] = value
	}
	updated["access_token"] = result.AccessToken
	if result.RefreshToken != "" {
		updated["refresh_token"] = result.RefreshToken
	}
	encrypted, err := credVault.Encrypt(updated)
	if err != nil {
		slog.Warn("rotation persist failed: encrypt", "account", credential.AccountID, "error", err)
		return false
	}
	expected := record.CredentialVersion
	expiresAt := ""
	if result.ExpiresIn > 0 {
		expiresAt = store.FormatISO(time.Now().UTC().Add(time.Duration(result.ExpiresIn) * time.Second))
	}
	write := store.CredentialWrite{
		Provider:         credential.Provider,
		AccountID:        credential.AccountID,
		Purpose:          credential.Purpose,
		Mode:             record.Mode,
		EncryptedPayload: encrypted,
		HasRefreshToken:  true,
		ExpectedVersion:  &expected,
	}
	if expiresAt != "" {
		write.ExpiresAt = &expiresAt
	}
	if _, err := db.UpsertCredential(ctx, write); err != nil {
		if errors.Is(err, store.VersionConflict) {
			// Another writer rotated it first; reloading is enough.
			slog.Info("rotation raced", "account", credential.AccountID)
			return false
		}
		slog.Warn("rotation persist failed", "account", credential.AccountID, "error", err)
		return false
	}
	// Mirror the deadline onto the purpose row so the console stops showing the
	// stale expiry; failure here is cosmetic because the credential is stored.
	if write.ExpiresAt != nil {
		if err := db.SetPurposeExpiry(ctx, credential.Provider, credential.AccountID, credential.Purpose, write.ExpiresAt); err != nil {
			slog.Warn("purpose expiry sync failed", "account", credential.AccountID, "error", err)
		}
	}
	if plane != nil {
		if err := plane.Reload(ctx, db); err != nil {
			slog.Warn("pool reload after rotation failed", "error", err)
		}
	}
	return true
}

// intlRefreshResult is the outcome of one refresh exchange.
type intlRefreshResult struct {
	AccessToken  string
	RefreshToken string
	ExpiresIn    int
}

// refreshIntlToken rotates the international bearer pair.
//
// Contract: POST {endpoint}/v2/plugin/auth/token/refresh with an empty JSON body
// and the refresh token in the X-Refresh-Token header. Upstream keeps the same
// refresh-token value, so only the access token normally changes.
func refreshIntlToken(ctx context.Context, refreshToken string) (intlRefreshResult, error) {
	endpoint := envOr("WORKBUDDY_INTL_ENDPOINT", "https://www.workbuddy.ai")
	url := endpoint + "/v2/plugin/auth/token/refresh"
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader([]byte("{}")))
	if err != nil {
		return intlRefreshResult{}, err
	}
	request.Header.Set("Accept", "application/json, text/plain, */*")
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("X-Domain", "www.workbuddy.ai")
	request.Header.Set("X-Refresh-Token", refreshToken)
	request.Header.Set("X-Auth-Refresh-Source", "plugin")
	request.Header.Set("User-Agent", "CLI/1.0.8 CodeBuddy/1.0.8")
	request.Header.Set("X-Product", "SaaS")
	request.Header.Set("X-Request-ID", randomToken(16))

	client := &http.Client{
		// Proxy env vars are deliberately ignored: a stale pooled connection
		// through the local TUN proxy cost 200+ second stalls in the Python
		// build, and this client exists for exactly that class of call.
		Transport: &http.Transport{Proxy: nil, MaxIdleConnsPerHost: 4},
		Timeout:   20 * time.Second,
	}
	response, err := client.Do(request)
	if err != nil {
		return intlRefreshResult{}, err
	}
	defer response.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(response.Body, 64*1024))
	if response.StatusCode != http.StatusOK {
		return intlRefreshResult{}, errors.New("auth_refresh_http_error")
	}
	var envelope struct {
		Code int             `json:"code"`
		Msg  string          `json:"msg"`
		Data json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(body, &envelope); err != nil {
		return intlRefreshResult{}, errors.New("auth_invalid_json")
	}
	if envelope.Code != 0 {
		if envelope.Msg == "" {
			envelope.Msg = "auth_failed"
		}
		return intlRefreshResult{}, errors.New(truncate(envelope.Msg, 200))
	}
	var data struct {
		AccessToken    string `json:"accessToken"`
		AccessTokenAlt string `json:"access_token"`
		RefreshToken   string `json:"refreshToken"`
		RefreshAlt     string `json:"refresh_token"`
		ExpiresIn      int    `json:"expiresIn"`
		ExpiresInAlt   int    `json:"expires_in"`
	}
	if err := json.Unmarshal(envelope.Data, &data); err != nil {
		return intlRefreshResult{}, errors.New("auth_invalid_json")
	}
	result := intlRefreshResult{
		AccessToken:  firstNonEmpty(data.AccessToken, data.AccessTokenAlt),
		RefreshToken: firstNonEmpty(data.RefreshToken, data.RefreshAlt),
		ExpiresIn:    firstNonZero(data.ExpiresIn, data.ExpiresInAlt),
	}
	if result.AccessToken == "" {
		return intlRefreshResult{}, errors.New("auth_no_token")
	}
	return result, nil
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if value != "" {
			return value
		}
	}
	return ""
}

func firstNonZero(values ...int) int {
	for _, value := range values {
		if value != 0 {
			return value
		}
	}
	return 0
}

func envOr(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}
