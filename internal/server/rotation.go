package server

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/config"
	"github.com/dmego/qoderbuddy2api/internal/models"
	"github.com/dmego/qoderbuddy2api/internal/store"
	"github.com/dmego/qoderbuddy2api/internal/vault"
)

// rotateOnce refreshes every credential whose expiry is inside the lead window.
//
// Both kept providers expose the same plugin-OAuth refresh contract, so both
// are rotated. The domestic pair was previously left alone on the belief that
// it had no refresh endpoint; it does, and the two calls differ only in origin
// and X-Domain, so the wiring is shared.
//
// A failed refresh is NOT treated as terminal. Measured 2026-10-06: upstream
// answers "12153 refresh token is invalid" intermittently for a refresh token
// that succeeds on the next attempt, and a refresh resurrects an access token
// whose Keycloak session was reported missing. Marking the account needs_reauth
// on a failed refresh would therefore disable automatic renewal for an account
// that would have recovered, so the loop keeps retrying and only the live
// request path (the metrics probe) may declare a credential dead.
//
// warned de-duplicates the per-credential warning: the loop runs every 15
// minutes, and a credential that keeps failing would otherwise emit the same
// line 96 times a day.
func rotateOnce(ctx context.Context, db *store.DB, credVault *vault.Vault, lead time.Duration, plane *ProxyPlane, settings config.Settings, warned map[string]bool) {
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
		key := credential.Provider + ":" + credential.AccountID + ":" + credential.Purpose
		switch credential.Provider {
		case models.ProviderWorkBuddyIntl, models.ProviderWorkBuddy:
			var rotated bool
			if credential.Provider == models.ProviderWorkBuddyIntl {
				rotated = rotateIntl(roundCtx, db, credVault, credential, plane, settings, warned)
			} else {
				rotated = rotateDomestic(roundCtx, db, credVault, credential, plane, settings, warned)
			}
			if rotated {
				// Clear the de-duplication marker so a later failure is reported
				// again instead of being suppressed by this success.
				delete(warned, key)
				slog.Info("rotated credential",
					"provider", credential.Provider, "account", credential.AccountID,
					"purpose", credential.Purpose)
			}
		default:
			// Nothing to do without a refresh contract; surface it so the
			// operator can re-import before the credential actually lapses.
			if expiresAt.Before(now) && !warned[key] {
				warned[key] = true
				slog.Warn("credential expired without a refresh contract",
					"provider", credential.Provider, "account", credential.AccountID,
					"purpose", credential.Purpose)
			}
		}
	}
}

// rotateDomestic refreshes one domestic WorkBuddy/CodeBuddy credential.
//
// The domestic deployment runs the same plugin refresh endpoint as the
// international one; only the origin and the X-Domain header differ. Its
// refresh token is a Keycloak offline session, so a refresh that upstream has
// invalidated ("invalid_grant: Offline user session not found") can never
// succeed and must be surfaced as a re-login, not retried as a transient fault.
func rotateDomestic(ctx context.Context, db *store.DB, credVault *vault.Vault, credential store.Credential, plane *ProxyPlane, settings config.Settings, warned map[string]bool) bool {
	endpoint := strings.TrimRight(settings.CodeBuddyEndpoint, "/")
	if endpoint == "" {
		endpoint = defaultCodeBuddyEndpoint
	}
	return rotateCredential(ctx, db, credVault, credential, plane, endpoint, "copilot.tencent.com", warned)
}

// rotateIntl refreshes one international credential through the same plugin
// contract the domestic deployment uses.
func rotateIntl(ctx context.Context, db *store.DB, credVault *vault.Vault, credential store.Credential, plane *ProxyPlane, settings config.Settings, warned map[string]bool) bool {
	endpoint := strings.TrimRight(settings.WorkBuddyIntlEndpoint, "/")
	if endpoint == "" {
		endpoint = defaultWorkBuddyIntlEndpoint
	}
	return rotateCredential(ctx, db, credVault, credential, plane, endpoint, "www.workbuddy.ai", warned)
}

// rotateCredential refreshes one credential through the plugin refresh contract
// shared by both WorkBuddy deployments and persists the new pair. It reports
// whether a rotation actually landed.
func rotateCredential(ctx context.Context, db *store.DB, credVault *vault.Vault, credential store.Credential, plane *ProxyPlane, endpoint, domain string, warned map[string]bool) bool {
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
	result, err := refreshPluginToken(ctx, endpoint, domain, refreshToken)
	if err != nil {
		// A refused refresh is reported but NOT turned into a purpose state.
		// Measured 2026-10-06: the same refresh token alternates between
		// "refresh token is invalid" and success, and a successful refresh
		// revives an access token whose session a live request had just
		// rejected. Declaring the account dead here would therefore switch off
		// automatic renewal for a credential that would have recovered; only
		// the request path (the metrics probe) may do that.
		//
		// warnOnce de-duplicates the line: this loop runs every 15 minutes.
		warnOnce(warned, credential.Provider+":"+credential.AccountID+":"+credential.Purpose,
			"token refresh failed",
			"provider", credential.Provider, "account", credential.AccountID,
			"purpose", credential.Purpose, "error", err)
		return false
	}
	if result.AccessToken == "" {
		warnOnce(warned, credential.Provider+":"+credential.AccountID+":"+credential.Purpose,
			"token refresh returned no access token", "account", credential.AccountID)
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
	// A landed rotation is proof the credential is alive again, so any rejection
	// recorded against this purpose was either transient or repaired. Without
	// this the account stays out of the proxy pool and out of every scheduler
	// that gates on status, even though its credential now works — which is
	// exactly the state a false-positive 401 would leave behind.
	revivePurpose(ctx, db, credential.Provider, credential.AccountID, credential.Purpose)
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

// revivePurpose returns a rejected purpose to active when a live call proves the
// credential works, and reloads the pool so the account re-enters rotation. It
// is a no-op for a purpose that is not currently rejected.
func revivePurpose(ctx context.Context, db *store.DB, provider, accountID, purpose string) {
	purposes, err := db.ListPurposes(ctx, provider, accountID)
	if err != nil {
		return
	}
	for _, row := range purposes {
		if row.Purpose != purpose || row.Status != "needs_reauth" {
			continue
		}
		if err := db.ClearPurposeRejection(ctx, provider, accountID, purpose); err != nil {
			slog.Warn("recovered credential could not be reactivated",
				"provider", provider, "account", accountID, "error", err)
			return
		}
		slog.Info("credential recovered; purpose reactivated",
			"provider", provider, "account", accountID, "purpose", purpose)
		return
	}
}

// defaultRefreshEndpoints mirror the config defaults so a settings value that
// was left empty (a bare .env deployment) still refreshes against the right
// origin instead of building a relative URL.
const (
	defaultCodeBuddyEndpoint     = "https://copilot.tencent.com"
	defaultWorkBuddyIntlEndpoint = "https://www.workbuddy.ai"
)

// warnOnce logs a warning at most once per key until the key is cleared. The
// rotation loop runs every 15 minutes, so a persistently failing credential
// would otherwise emit the same line 96 times a day and bury the signal.
func warnOnce(seen map[string]bool, key, message string, args ...any) {
	if seen == nil {
		slog.Warn(message, args...)
		return
	}
	if seen[key] {
		return
	}
	seen[key] = true
	slog.Warn(message, args...)
}

// pluginRefreshResult is the outcome of one refresh exchange.
type pluginRefreshResult struct {
	AccessToken  string
	RefreshToken string
	ExpiresIn    int
}

// refreshPluginToken rotates a WorkBuddy bearer pair.
//
// Contract (both deployments): POST {endpoint}/v2/plugin/auth/token/refresh
// with an empty JSON body and the refresh token in the X-Refresh-Token header.
// Upstream issues a new access token and, for Keycloak-backed sessions, also
// rotates the refresh token, so the caller must persist both.
//
// A 12153 body is not by itself proof that the session is gone: upstream
// returns it intermittently for a refresh token that succeeds moments later, so
// the caller reports the failure and retries rather than marking the account
// dead.
func refreshPluginToken(ctx context.Context, endpoint, domain, refreshToken string) (pluginRefreshResult, error) {
	url := strings.TrimRight(endpoint, "/") + "/v2/plugin/auth/token/refresh"
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader([]byte("{}")))
	if err != nil {
		return pluginRefreshResult{}, err
	}
	request.Header.Set("Accept", "application/json, text/plain, */*")
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("X-Domain", domain)
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
		return pluginRefreshResult{}, err
	}
	defer response.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(response.Body, 64*1024))
	var envelope struct {
		Code int             `json:"code"`
		Msg  string          `json:"msg"`
		Data json.RawMessage `json:"data"`
	}
	// The rejection body is JSON even on a non-200, so parse before judging the
	// status: it is what separates a dead session from a transport-level fault.
	if err := json.Unmarshal(body, &envelope); err != nil {
		if response.StatusCode != http.StatusOK {
			return pluginRefreshResult{}, errors.New("auth_refresh_http_error")
		}
		return pluginRefreshResult{}, errors.New("auth_invalid_json")
	}
	if envelope.Code != 0 {
		message := envelope.Msg
		if message == "" {
			message = "auth_failed"
		}
		return pluginRefreshResult{}, errors.New(truncate(message, 200))
	}
	if response.StatusCode != http.StatusOK {
		return pluginRefreshResult{}, errors.New("auth_refresh_http_error")
	}
	if envelope.Code != 0 {
		if envelope.Msg == "" {
			envelope.Msg = "auth_failed"
		}
		return pluginRefreshResult{}, errors.New(truncate(envelope.Msg, 200))
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
		return pluginRefreshResult{}, errors.New("auth_invalid_json")
	}
	result := pluginRefreshResult{
		AccessToken:  firstNonEmpty(data.AccessToken, data.AccessTokenAlt),
		RefreshToken: firstNonEmpty(data.RefreshToken, data.RefreshAlt),
		ExpiresIn:    firstNonZero(data.ExpiresIn, data.ExpiresInAlt),
	}
	if result.AccessToken == "" {
		return pluginRefreshResult{}, errors.New("auth_no_token")
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
