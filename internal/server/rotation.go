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
	"sync"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/config"
	"github.com/dmego/qoderbuddy2api/internal/models"
	"github.com/dmego/qoderbuddy2api/internal/store"
	"github.com/dmego/qoderbuddy2api/internal/vault"
)

// rotationPolicy decides when one credential is refreshed.
//
// Rotation used to be expiry-driven only: refresh when the stored deadline is
// within the lead window. That rule never fired here, because WorkBuddy hands
// out access tokens with a 28-day exp while the session behind them can be
// revoked after a few days. Measured 2026-10-17: all four domestic accounts
// carried exp=2026-11-13 and were refused with 401 eleven days after signing
// in, so every request in the meantime was served by a credential the loop had
// decided needed no attention.
//
// The policy has three triggers, and rollingRefresh is the one that matters:
//
//   - rolling: refresh at a fixed interval regardless of expiry, which keeps
//     the offline session warm and bounds how stale a stored credential can
//     be when upstream revokes sessions;
//   - proportional: refresh once a fraction of the credential's own lifetime
//     remains, which adapts to a 2-hour token and a 28-day one alike;
//   - absolute: the historical fixed lead, kept as a floor.
type rotationPolicy struct {
	// interval is the rolling trigger: zero disables it.
	interval time.Duration
	// lead is the absolute floor.
	lead time.Duration
	// fraction is the share of the credential's lifetime that must remain
	// before the proportional trigger fires; zero disables it.
	fraction float64
}

// defaultRotationFraction is the proportional share. A 28-day credential starts
// refreshing with 5.6 days left, while a 2-hour one starts with 24 minutes left.
const defaultRotationFraction = 0.2

// deadSessionRetry is how long a revoked session waits before the next probe.
//
// It follows the rolling interval so the cost of keeping a dead account in the
// retry set is bounded by the operator's own setting, with a floor for the case
// where the rolling trigger is disabled.
func (policy rotationPolicy) deadSessionRetry() time.Duration {
	if policy.interval > 0 {
		return policy.interval
	}
	return 6 * time.Hour
}

// refreshDue reports whether a credential should be refreshed now.
//
// A credential with no parseable expiry is still refreshed on the rolling
// interval: an unknown deadline is exactly the case where waiting for it means
// waiting forever.
func (policy rotationPolicy) refreshDue(updatedAt time.Time, expiresAt time.Time, hasExpiry bool, now time.Time) bool {
	if policy.interval > 0 && !updatedAt.IsZero() && now.Sub(updatedAt) >= policy.interval {
		return true
	}
	if !hasExpiry {
		return false
	}
	remaining := expiresAt.Sub(now)
	if policy.lead > 0 && remaining <= policy.lead {
		return true
	}
	if policy.fraction > 0 {
		// The credential's own lifetime: how long it was meant to last, from
		// the moment it was written. A refreshed row has both stamps, so the
		// window follows the current token rather than the first ever issued.
		lifetime := expiresAt.Sub(updatedAt)
		if lifetime > 0 && remaining <= time.Duration(policy.fraction*float64(lifetime)) {
			return true
		}
	}
	return false
}

// rotateOnce refreshes every credential the policy says is due.
//
// Both kept providers expose the same plugin-OAuth refresh contract, so both
// are rotated. The domestic pair was previously left alone on the belief that
// it had no refresh endpoint; it does, and the two calls differ only in origin
// and X-Domain, so the wiring is shared.
//
// Failure handling is graded by classifyRefreshFailure, because the upstream
// endpoint reports two very different things:
//
//   - a transient fault (network, 5xx, the bare "12153 refresh token failed"
//     body measured to clear on its own) only logs. Marking the account dead
//     here would switch off renewal for a credential that would have recovered.
//   - a dead session ("Refresh token issued before the user session started",
//     "Offline user session not found") moves the purpose to needs_reauth so
//     the console shows it and the pool drops the account. The retry policy is
//     deliberately forgiving: the loop keeps probing the same credential every
//     interval, so a session that upstream later revives is picked up without
//     an operator doing anything, and a permanently dead one costs one call per
//     interval per account.
//
// warned de-duplicates the per-credential warning: the loop runs every few
// minutes, and a credential that keeps failing would otherwise emit the same
// line hundreds of times a day.
func rotateOnce(ctx context.Context, db *store.DB, credVault *vault.Vault, policy rotationPolicy, plane *ProxyPlane, settings config.Settings, warned map[string]bool, held map[string]time.Time) rotationRound {
	roundCtx, cancel := context.WithTimeout(ctx, 2*time.Minute)
	defer cancel()
	credentials, err := db.ListCredentials(roundCtx, models.KnownProviders)
	if err != nil {
		slog.Warn("credential rotation lookup failed", "error", err)
		return rotationRound{}
	}
	round := rotationRound{retryAfter: map[string]time.Duration{}}
	now := time.Now().UTC()
	for _, credential := range credentials {
		if !credential.HasRefreshToken {
			continue
		}
		updatedAt, _ := store.ParseISO(credential.UpdatedAt)
		expiresAt, hasExpiry := time.Time{}, false
		if credential.ExpiresAt != nil {
			if parsed, ok := store.ParseISO(*credential.ExpiresAt); ok {
				expiresAt, hasExpiry = parsed, true
			}
		}
		if !policy.refreshDue(updatedAt, expiresAt, hasExpiry, now) {
			continue
		}
		key := credential.Provider + ":" + credential.AccountID + ":" + credential.Purpose
		if until, ok := held[key]; ok && now.Before(until) {
			continue
		}
		switch credential.Provider {
		case models.ProviderWorkBuddyIntl, models.ProviderWorkBuddy:
			outcome := refreshOutcome{}
			if credential.Provider == models.ProviderWorkBuddyIntl {
				outcome = rotateIntl(roundCtx, db, credVault, credential, plane, settings, warned)
			} else {
				outcome = rotateDomestic(roundCtx, db, credVault, credential, plane, settings, warned)
			}
			switch {
			case outcome.rotated:
				// Clear the de-duplication marker so a later failure is
				// reported again instead of being suppressed by this success.
				delete(warned, key)
				slog.Info("rotated credential",
					"provider", credential.Provider, "account", credential.AccountID,
					"purpose", credential.Purpose)
			case outcome.kind == refreshSessionDead:
				reportDeadSession(roundCtx, db, credential, outcome, warned)
				// A revoked session is not worth probing at the scan cadence.
				// Space the next attempt by the rolling interval: that keeps the
				// recovery path open (upstream occasionally restores a session)
				// at the cost of one call per interval instead of one per scan.
				round.retryAfter[key] = policy.deadSessionRetry()
			}
		default:
			// Nothing to do without a refresh contract; surface it so the
			// operator can re-import before the credential actually lapses.
			if hasExpiry && expiresAt.Before(now) && !warned[key] {
				warned[key] = true
				slog.Warn("credential expired without a refresh contract",
					"provider", credential.Provider, "account", credential.AccountID,
					"purpose", credential.Purpose)
			}
		}
	}
	return round
}

// rotationRound reports what one pass over the credentials produced.
//
// retryAfter lets a caller space out the next attempt for credentials that
// failed in a way that will not change on the next scan. A revoked session is
// the case that matters: retrying it every 15 minutes would put ~100 pointless
// calls per account per day against an endpoint that has already answered.
type rotationRound struct {
	retryAfter map[string]time.Duration
}

// reportDeadSession records an unrecoverable session and logs it once.
//
// The credential is NOT deleted and the purpose is not disabled: a revoked
// session is repaired by signing in again, and the rotation loop keeps probing
// so that a session upstream later restores needs no operator action.
func reportDeadSession(ctx context.Context, db *store.DB, credential store.Credential, outcome refreshOutcome, warned map[string]bool) {
	key := credential.Provider + ":" + credential.AccountID + ":" + credential.Purpose
	if !warned[key] {
		warned[key] = true
		slog.Warn("credential session revoked upstream; re-login required",
			"provider", credential.Provider, "account", credential.AccountID,
			"purpose", credential.Purpose, "error", outcome.err)
	}
	if err := db.MarkPurposeRejected(ctx, credential.Provider, credential.AccountID, credential.Purpose, "session_revoked"); err != nil {
		slog.Warn("could not record revoked session",
			"provider", credential.Provider, "account", credential.AccountID, "error", err)
	}
}

// rotateDomestic refreshes one domestic WorkBuddy/CodeBuddy credential.
//
// The domestic deployment runs the same plugin refresh endpoint as the
// international one; only the origin and the X-Domain header differ. Its
// refresh token is a Keycloak offline session, so a refresh that upstream has
// invalidated ("invalid_grant: Offline user session not found") can never
// succeed and must be surfaced as a re-login, not retried as a transient fault.
func rotateDomestic(ctx context.Context, db *store.DB, credVault *vault.Vault, credential store.Credential, plane *ProxyPlane, settings config.Settings, warned map[string]bool) refreshOutcome {
	endpoint := strings.TrimRight(settings.CodeBuddyEndpoint, "/")
	if endpoint == "" {
		endpoint = defaultCodeBuddyEndpoint
	}
	return rotateCredential(ctx, db, credVault, credential, plane, endpoint, "copilot.tencent.com", warned)
}

// rotateIntl refreshes one international credential through the same plugin
// contract the domestic deployment uses.
func rotateIntl(ctx context.Context, db *store.DB, credVault *vault.Vault, credential store.Credential, plane *ProxyPlane, settings config.Settings, warned map[string]bool) refreshOutcome {
	endpoint := strings.TrimRight(settings.WorkBuddyIntlEndpoint, "/")
	if endpoint == "" {
		endpoint = defaultWorkBuddyIntlEndpoint
	}
	return rotateCredential(ctx, db, credVault, credential, plane, endpoint, "www.workbuddy.ai", warned)
}

// refreshOutcome is what one rotation attempt achieved.
type refreshOutcome struct {
	rotated bool
	kind    refreshFailure
	err     error
}

// rotateCredential refreshes one credential through the plugin refresh contract
// shared by both WorkBuddy deployments and persists the new pair.
//
// A successful refresh overwrites both tokens and the deadline. A failed one
// never touches the stored credential: the access token already in the row is
// usually still usable, and replacing it with nothing would take a working
// account out of service to fix a refresh problem.
func rotateCredential(ctx context.Context, db *store.DB, credVault *vault.Vault, credential store.Credential, plane *ProxyPlane, endpoint, domain string, warned map[string]bool) refreshOutcome {
	record, err := db.GetCredential(ctx, credential.Provider, credential.AccountID, credential.Purpose)
	if err != nil {
		slog.Warn("rotation skipped: credential row unreadable", "account", credential.AccountID, "error", err)
		return refreshOutcome{kind: refreshTransient, err: err}
	}
	payload, err := credVault.Decrypt(record.EncryptedPayload)
	if err != nil {
		slog.Warn("rotation skipped: credential undecryptable", "account", credential.AccountID, "error", err)
		return refreshOutcome{kind: refreshRejected, err: err}
	}
	refreshToken, _ := payload["refresh_token"].(string)
	if refreshToken == "" {
		if !warned[credential.Provider+":"+credential.AccountID] {
			warned[credential.Provider+":"+credential.AccountID] = true
			slog.Warn("rotation skipped: no refresh token", "account", credential.AccountID)
		}
		return refreshOutcome{kind: refreshRejected, err: errors.New("no refresh token stored")}
	}
	result, err := refreshPluginToken(ctx, endpoint, domain, refreshToken)
	if err != nil {
		kind := classifyRefreshFailure(err)
		// Only transient faults are logged here; a revoked session is reported
		// by the caller with its own wording and its own state transition.
		if kind == refreshTransient {
			warnOnce(warned, credential.Provider+":"+credential.AccountID+":"+credential.Purpose,
				"token refresh failed",
				"provider", credential.Provider, "account", credential.AccountID,
				"purpose", credential.Purpose, "error", err)
		}
		return refreshOutcome{kind: kind, err: err}
	}
	if result.AccessToken == "" {
		warnOnce(warned, credential.Provider+":"+credential.AccountID+":"+credential.Purpose,
			"token refresh returned no access token", "account", credential.AccountID)
		return refreshOutcome{kind: refreshTransient, err: errors.New("auth_no_token")}
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
		return refreshOutcome{kind: refreshTransient, err: err}
	}
	expected := record.CredentialVersion
	write := store.CredentialWrite{
		Provider:         credential.Provider,
		AccountID:        credential.AccountID,
		Purpose:          credential.Purpose,
		Mode:             record.Mode,
		EncryptedPayload: encrypted,
		HasRefreshToken:  true,
		ExpectedVersion:  &expected,
	}
	// Keep the previous deadline when the response omits expiresIn. Writing an
	// empty value would leave the row without one, and the rolling trigger would
	// then be the only thing standing between the account and a refresh storm.
	expiresAt := credential.ExpiresAt
	if result.ExpiresIn > 0 {
		formatted := store.FormatISO(time.Now().UTC().Add(time.Duration(result.ExpiresIn) * time.Second))
		expiresAt = &formatted
	}
	if expiresAt != nil {
		write.ExpiresAt = expiresAt
	}
	if _, err := db.UpsertCredential(ctx, write); err != nil {
		if errors.Is(err, store.VersionConflict) {
			// Another writer rotated it first; reloading is enough.
			slog.Info("rotation raced", "account", credential.AccountID)
			return refreshOutcome{kind: refreshTransient}
		}
		slog.Warn("rotation persist failed", "account", credential.AccountID, "error", err)
		return refreshOutcome{kind: refreshTransient, err: err}
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
	return refreshOutcome{rotated: true}
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

// authRescueCooldown bounds how often one account may be rescued, so a pool-wide
// 401 storm produces one refresh per account instead of one per failed request.
const authRescueCooldown = 60 * time.Second

// authRescue refreshes a credential the moment upstream refuses its token.
//
// The scheduled loop alone is not enough: it runs on a scan tick, so a token
// refused one second after a round would stay broken for the whole interval.
// A 401 is unambiguous and cheap to repair, and the refresh token is usually
// still valid, so the request path reports the account here and the repair
// happens out of band while the pool fails over to another account.
type authRescue struct {
	db        *store.DB
	credVault *vault.Vault
	plane     *ProxyPlane
	settings  config.Settings

	mu       sync.Mutex
	attempts map[string]time.Time
}

// newAuthRescue builds the rescue hook and installs it on both WorkBuddy pools.
func newAuthRescue(db *store.DB, credVault *vault.Vault, plane *ProxyPlane, settings config.Settings) *authRescue {
	rescue := &authRescue{
		db:        db,
		credVault: credVault,
		plane:     plane,
		settings:  settings,
		attempts:  map[string]time.Time{},
	}
	if plane != nil {
		plane.SetAuthRejectedHook(rescue.trigger)
	}
	return rescue
}

// trigger starts a repair unless this account was already attempted recently.
func (r *authRescue) trigger(provider, accountID string) {
	if provider != models.ProviderWorkBuddy && provider != models.ProviderWorkBuddyIntl {
		// Other providers have no refresh contract; a 401 there needs a re-login
		// and the scheduled loop already reports it.
		return
	}
	key := provider + ":" + accountID
	r.mu.Lock()
	if last, ok := r.attempts[key]; ok && time.Since(last) < authRescueCooldown {
		r.mu.Unlock()
		return
	}
	r.attempts[key] = time.Now()
	r.mu.Unlock()
	go r.refresh(provider, accountID)
}

// refresh rotates every refreshable credential of one account.
func (r *authRescue) refresh(provider, accountID string) {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	credentials, err := r.db.ListCredentials(ctx, []string{provider})
	if err != nil {
		slog.Warn("auth rescue lookup failed", "provider", provider, "account", accountID, "error", err)
		return
	}
	// A fresh map per rescue: the cooldown above already bounds log volume, and
	// sharing the scheduler's map would race with its goroutine.
	warned := map[string]bool{}
	for _, credential := range credentials {
		if credential.AccountID != accountID || !credential.HasRefreshToken {
			continue
		}
		outcome := refreshOutcome{}
		if provider == models.ProviderWorkBuddyIntl {
			outcome = rotateIntl(ctx, r.db, r.credVault, credential, r.plane, r.settings, warned)
		} else {
			outcome = rotateDomestic(ctx, r.db, r.credVault, credential, r.plane, r.settings, warned)
		}
		switch {
		case outcome.rotated:
			slog.Info("rescued refused credential",
				"provider", provider, "account", accountID, "purpose", credential.Purpose)
		case outcome.kind == refreshSessionDead:
			reportDeadSession(ctx, r.db, credential, outcome, warned)
		}
	}
}
