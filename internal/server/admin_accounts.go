package server

import (
	"context"
	"errors"
	"net/http"
	"sort"
	"strings"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/models"
	"github.com/dmego/qoderbuddy2api/internal/providers"
	"github.com/dmego/qoderbuddy2api/internal/store"
)

// accountView is the JSON shape the accounts page and account detail page
// consume. The purposes field is a MAP keyed by purpose name (not a list) and
// the aggregate status field is named summary_status — both are pinned by the
// Vue type literals in AccountsPage.vue and AccountDetailPage.vue.
type accountView struct {
	Provider       string                 `json:"provider"`
	AccountID      string                 `json:"account_id"`
	Label          string                 `json:"label"`
	Source         string                 `json:"source"`
	Enabled        bool                   `json:"enabled"`
	SummaryStatus  string                 `json:"summary_status"`
	MaskedIdentity *string                `json:"masked_identity"`
	Shadowed       bool                   `json:"shadowed"`
	Purposes       map[string]purposeView `json:"purposes"`
	CreatedAt      string                 `json:"created_at"`
	UpdatedAt      string                 `json:"updated_at"`
}

// purposeView is one entry of the purposes map.
type purposeView struct {
	Enabled            bool    `json:"enabled"`
	Status             string  `json:"status"`
	VerificationStatus string  `json:"verification_status"`
	ExpiresAt          *string `json:"expires_at"`
	VerifiedAt         *string `json:"verified_at"`
	LastSuccessAt      *string `json:"last_success_at"`
	FailureCount       int     `json:"failure_count"`
	LastError          *string `json:"last_error"`
}

// buildAccountView joins an account row with its purposes.
//
// The aggregate status is derived from the chat purpose when present, because
// that is the credential every request path depends on; a disabled account
// overrides it so the table never shows a disabled row as healthy.
func buildAccountView(account store.Account, purposes []store.Purpose) accountView {
	view := accountView{
		Provider:       account.Provider,
		AccountID:      account.AccountID,
		Label:          account.Label,
		Source:         account.Source,
		Enabled:        account.Enabled,
		SummaryStatus:  "unconfigured",
		MaskedIdentity: account.MaskedIdentity,
		Purposes:       map[string]purposeView{},
		CreatedAt:      account.CreatedAt,
		UpdatedAt:      account.UpdatedAt,
	}
	for _, purpose := range purposes {
		view.Purposes[purpose.Purpose] = purposeView{
			Enabled:            purpose.Enabled,
			Status:             purpose.Status,
			VerificationStatus: purpose.VerificationStatus,
			ExpiresAt:          purpose.ExpiresAt,
			VerifiedAt:         purpose.VerifiedAt,
			LastSuccessAt:      purpose.LastSuccessAt,
			FailureCount:       purpose.FailureCount,
			LastError:          purpose.LastError,
		}
	}
	if chat, ok := view.Purposes["chat"]; ok {
		view.SummaryStatus = chat.Status
	}
	if !account.Enabled {
		view.SummaryStatus = "disabled"
	}
	return view
}

func (a *API) handleListAccounts(w http.ResponseWriter, r *http.Request) {
	query := r.URL.Query()
	accounts, err := a.DB.ListAccounts(r.Context(), models.KnownProviders)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	purposes, err := a.DB.ListAllPurposes(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	byAccount := map[string][]store.Purpose{}
	for _, purpose := range purposes {
		key := purpose.Provider + ":" + purpose.AccountID
		byAccount[key] = append(byAccount[key], purpose)
	}

	providerFilter := query.Get("provider")
	sourceFilter := query.Get("source")
	statusFilter := query.Get("status")
	purposeFilter := query.Get("purpose")
	search := strings.ToLower(strings.TrimSpace(query.Get("query")))

	views := make([]accountView, 0, len(accounts))
	for _, account := range accounts {
		view := buildAccountView(account, byAccount[account.Provider+":"+account.AccountID])
		if providerFilter != "" && view.Provider != providerFilter {
			continue
		}
		if sourceFilter != "" && view.Source != sourceFilter {
			continue
		}
		if statusFilter != "" && view.SummaryStatus != statusFilter {
			continue
		}
		if purposeFilter != "" && !hasPurpose(view, purposeFilter) {
			continue
		}
		if search != "" &&
			!strings.Contains(strings.ToLower(view.AccountID), search) &&
			!strings.Contains(strings.ToLower(view.Label), search) {
			continue
		}
		views = append(views, view)
	}
	sort.SliceStable(views, func(i, j int) bool {
		if views[i].Provider != views[j].Provider {
			return views[i].Provider < views[j].Provider
		}
		return views[i].AccountID < views[j].AccountID
	})
	limit, offset, err := pageFromRequest(r, 20)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	total := len(views)
	page := paginate(views, offset, limit)
	writeJSON(w, http.StatusOK, map[string]any{
		"accounts":    list(page),
		"next_cursor": nextCursor(offset, limit, len(page)),
		"total":       total,
	})
}

func hasPurpose(view accountView, purpose string) bool {
	candidate, ok := view.Purposes[purpose]
	return ok && candidate.Enabled
}

// paginate slices a view list by offset and limit.
func paginate[T any](values []T, offset, limit int) []T {
	if offset < 0 {
		offset = 0
	}
	if offset >= len(values) {
		return []T{}
	}
	end := offset + limit
	if end > len(values) {
		end = len(values)
	}
	return values[offset:end]
}

func (a *API) handleGetAccount(w http.ResponseWriter, r *http.Request) {
	provider := r.PathValue("provider")
	accountID := r.PathValue("accountID")
	account, err := a.DB.GetAccount(r.Context(), provider, accountID)
	if err != nil {
		writeError(w, http.StatusNotFound, "account not found")
		return
	}
	purposes, err := a.DB.ListPurposes(r.Context(), provider, accountID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	view := buildAccountView(account, purposes)
	writeJSON(w, http.StatusOK, view)
}

func (a *API) handleCreateAccount(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Provider  string `json:"provider"`
		AccountID string `json:"account_id"`
		Label     string `json:"label"`
		Source    string `json:"source"`
		Token     string `json:"token"`
		Refresh   string `json:"refresh_token"`
		Mode      string `json:"mode"`
		ExpiresAt string `json:"expires_at"`
		Enabled   *bool  `json:"enabled"`
	}
	if err := decodeBody(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	if !models.ChatOnlyProviders[body.Provider] && body.Provider != models.ProviderWorkBuddy {
		writeError(w, http.StatusBadRequest, "unsupported provider: "+body.Provider)
		return
	}
	if body.Token == "" {
		writeError(w, http.StatusBadRequest, "token is required")
		return
	}
	enabled := true
	if body.Enabled != nil {
		enabled = *body.Enabled
	}
	account, err := a.DB.UpsertAccount(r.Context(), store.Account{
		Provider:  body.Provider,
		AccountID: body.AccountID,
		Label:     body.Label,
		Source:    orDefaultString(body.Source, "manual"),
		Enabled:   enabled,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	payload := map[string]any{"access_token": body.Token}
	if body.Refresh != "" {
		payload["refresh_token"] = body.Refresh
	}
	if err := a.storeCredential(r, body.Provider, account.AccountID, "chat", orDefaultString(body.Mode, "bearer"), payload, body.ExpiresAt); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if err := a.upsertChatPurpose(r, body.Provider, account.AccountID, body.ExpiresAt); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	a.audit(r, "account.create", "account", body.Provider+"/"+account.AccountID, nil)
	if err := a.refreshPlane(r); err != nil {
		writeError(w, http.StatusServiceUnavailable, "provider_pool_refresh_failed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "created", "account": account})
}

func (a *API) handlePatchAccount(w http.ResponseWriter, r *http.Request) {
	provider := r.PathValue("provider")
	accountID := r.PathValue("accountID")
	var body struct {
		Enabled *bool   `json:"enabled"`
		Label   *string `json:"label"`
	}
	if err := decodeBody(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	if err := a.DB.UpdateAccountFields(r.Context(), provider, accountID, body.Enabled, body.Label); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	a.audit(r, "account.update", "account", provider+"/"+accountID, nil)
	if err := a.refreshPlane(r); err != nil {
		writeError(w, http.StatusServiceUnavailable, "provider_pool_refresh_failed")
		return
	}
	account, err := a.DB.GetAccount(r.Context(), provider, accountID)
	if err != nil {
		writeError(w, http.StatusNotFound, "account not found")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "updated", "account": account})
}

func (a *API) handleDeleteAccount(w http.ResponseWriter, r *http.Request) {
	provider := r.PathValue("provider")
	accountID := r.PathValue("accountID")
	if err := a.DB.DeleteAccount(r.Context(), provider, accountID); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	a.audit(r, "account.delete", "account", provider+"/"+accountID, nil)
	if err := a.refreshPlane(r); err != nil {
		writeError(w, http.StatusServiceUnavailable, "provider_pool_refresh_failed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "deleted", "provider": provider, "account_id": accountID,
	})
}

func (a *API) handleRefreshAccount(w http.ResponseWriter, r *http.Request) {
	provider := r.PathValue("provider")
	accountID := r.PathValue("accountID")
	if err := a.refreshPlane(r); err != nil {
		writeError(w, http.StatusServiceUnavailable, "provider_pool_refresh_failed")
		return
	}
	view := accountView{Provider: provider, AccountID: accountID, SummaryStatus: "refreshed"}
	writeJSON(w, http.StatusOK, map[string]any{"status": "refreshed", "account": view})
}

// handleProbeAccount performs a live reachability check against the upstream
// using the account's stored credential.
func (a *API) handleProbeAccount(w http.ResponseWriter, r *http.Request) {
	provider := r.PathValue("provider")
	accountID := r.PathValue("accountID")
	result := a.probeAccount(r, provider, accountID)
	status := "ok"
	code := http.StatusOK
	if !result.OK {
		status = "failed"
		code = http.StatusOK
	}
	writeJSON(w, code, map[string]any{
		"status":     status,
		"provider":   provider,
		"account_id": accountID,
		"detail":     result.Detail,
		"latency_ms": result.LatencyMS,
	})
}

func (a *API) handlePromoteAccount(w http.ResponseWriter, r *http.Request) {
	provider := r.PathValue("provider")
	accountID := r.PathValue("accountID")
	var body struct {
		Label string `json:"label"`
	}
	_ = decodeBody(r, &body)
	if body.Label != "" {
		if err := a.DB.UpdateAccountFields(r.Context(), provider, accountID, nil, &body.Label); err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
	}
	source := "manual"
	if err := a.DB.UpdateAccountFields(r.Context(), provider, accountID, nil, nil); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	_ = source
	a.audit(r, "account.promote", "account", provider+"/"+accountID, nil)
	if err := a.refreshPlane(r); err != nil {
		writeError(w, http.StatusServiceUnavailable, "provider_pool_refresh_failed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "promoted", "provider": provider, "account_id": accountID,
	})
}

// handleVerifyCheckin re-runs the check-in status preflight for one account.
func (a *API) handleVerifyCheckin(w http.ResponseWriter, r *http.Request) {
	provider := r.PathValue("provider")
	accountID := r.PathValue("accountID")
	state, err := a.DB.GetCheckinDailyState(r.Context(), provider, accountID, a.localDate(), a.Settings.CheckinTimezone)
	if err != nil {
		writeJSON(w, http.StatusOK, map[string]any{
			"status": "unverified", "verified": false, "terminal_outcome": nil,
		})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status":           "verified",
		"verified":         state.TerminalOutcome != nil,
		"terminal_outcome": state.TerminalOutcome,
	})
}

// handleRederiveCheckin recomputes the daily state from the latest batch rows.
func (a *API) handleRederiveCheckin(w http.ResponseWriter, r *http.Request) {
	provider := r.PathValue("provider")
	accountID := r.PathValue("accountID")
	attempt, err := a.DB.LatestCheckinAttempt(r.Context(), provider, accountID)
	if err != nil {
		writeJSON(w, http.StatusOK, map[string]any{"status": "no_attempts", "outcome": nil})
		return
	}
	if attempt.Outcome != nil && (*attempt.Outcome == "CLAIMED" || *attempt.Outcome == "ALREADY_CHECKED_IN") {
		_ = a.DB.UpsertCheckinDailyState(r.Context(), store.CheckinDailyState{
			Provider:        provider,
			AccountID:       accountID,
			LocalDate:       a.localDate(),
			Timezone:        a.Settings.CheckinTimezone,
			TerminalOutcome: attempt.Outcome,
			LastRunID:       &attempt.RunID,
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "rederived", "outcome": attempt.Outcome, "run_id": attempt.RunID,
	})
}

// ---- credentials ----

func (a *API) handleListCredentials(w http.ResponseWriter, r *http.Request) {
	provider := r.URL.Query().Get("provider")
	providers := models.KnownProviders
	if provider != "" {
		providers = []string{provider}
	}
	credentials, err := a.DB.ListCredentials(r.Context(), providers)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	views := make([]map[string]any, 0, len(credentials))
	for _, credential := range credentials {
		views = append(views, credentialView(credential))
	}
	writeJSON(w, http.StatusOK, map[string]any{"credentials": list(views)})
}

// credentialView renders credential metadata. The expiry state is judged
// relative to the credential's own lifetime: a 2-hour token that was rotated a
// minute ago is not "expiring soon", while a 45-day token in its last 10% is.
func credentialView(credential store.Credential) map[string]any {
	view := map[string]any{
		"provider":           credential.Provider,
		"account_id":         credential.AccountID,
		"purpose":            credential.Purpose,
		"mode":               credential.Mode,
		"payload_version":    credential.PayloadVersion,
		"credential_version": credential.CredentialVersion,
		"has_refresh_token":  credential.HasRefreshToken,
		"expires_at":         credential.ExpiresAt,
		"updated_at":         credential.UpdatedAt,
		"expiry_state":       "unknown",
	}
	if credential.ExpiresAt != nil {
		expiresAt, ok := store.ParseISO(*credential.ExpiresAt)
		if ok {
			remaining := time.Until(expiresAt)
			switch {
			case remaining <= 0:
				view["expiry_state"] = "expired"
			default:
				lifetime := remaining
				if issuedAt, ok := store.ParseISO(credential.UpdatedAt); ok {
					if span := expiresAt.Sub(issuedAt); span > 0 {
						lifetime = span
					}
				}
				if lifetime > 0 && remaining.Seconds()/lifetime.Seconds() <= 0.1 {
					view["expiry_state"] = "expiring"
				} else {
					view["expiry_state"] = "valid"
				}
			}
		}
	}
	return view
}

func (a *API) handleRotateCredential(w http.ResponseWriter, r *http.Request) {
	provider := r.PathValue("provider")
	accountID := r.PathValue("accountID")
	purpose := r.PathValue("purpose")
	var body struct {
		Token     string `json:"token"`
		Cookie    string `json:"cookie"`
		Mode      string `json:"mode"`
		Refresh   string `json:"refresh_token"`
		ExpiresAt string `json:"expires_at"`
		Version   *int   `json:"credential_version"`
	}
	if err := decodeBody(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	if body.Token == "" && body.Cookie == "" {
		writeError(w, http.StatusBadRequest, "token or cookie is required")
		return
	}
	payload := map[string]any{}
	if body.Token != "" {
		payload["access_token"] = body.Token
	}
	if body.Cookie != "" {
		payload["cookie"] = body.Cookie
	}
	if body.Refresh != "" {
		payload["refresh_token"] = body.Refresh
	}
	record, err := a.DB.GetCredential(r.Context(), provider, accountID, purpose)
	expected := body.Version
	if expected == nil && err == nil {
		version := record.CredentialVersion
		expected = &version
	}
	mode := body.Mode
	if mode == "" {
		mode = "bearer"
	}
	fingerprint := body.Token
	if fingerprint == "" {
		fingerprint = body.Cookie
	}
	encrypted, err := a.Vault.Encrypt(payload)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	digest := a.Vault.Fingerprint(fingerprint)
	write := store.CredentialWrite{
		Provider:         provider,
		AccountID:        accountID,
		Purpose:          purpose,
		Mode:             mode,
		EncryptedPayload: encrypted,
		FingerprintHMAC:  &digest,
		ExpectedVersion:  expected,
	}
	if body.ExpiresAt != "" {
		expires := body.ExpiresAt
		write.ExpiresAt = &expires
	}
	write.HasRefreshToken = body.Refresh != "" || provider == models.ProviderWorkBuddyIntl
	version, err := a.DB.UpsertCredential(r.Context(), write)
	if errors.Is(err, store.VersionConflict) {
		writeError(w, http.StatusConflict, "credential_version_conflict")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if write.ExpiresAt != nil {
		_ = a.DB.SetPurposeExpiry(r.Context(), provider, accountID, purpose, write.ExpiresAt)
	}
	a.audit(r, "credential.rotate", "credential", provider+"/"+accountID+"/"+purpose, nil)
	if err := a.refreshPlane(r); err != nil {
		writeError(w, http.StatusServiceUnavailable, "provider_pool_refresh_failed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "rotated", "credential_version": version})
}

func (a *API) handleRevokeCredential(w http.ResponseWriter, r *http.Request) {
	provider := r.PathValue("provider")
	accountID := r.PathValue("accountID")
	purpose := r.PathValue("purpose")
	if err := a.DB.DeleteCredential(r.Context(), provider, accountID, purpose); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	a.audit(r, "credential.revoke", "credential", provider+"/"+accountID+"/"+purpose, nil)
	if err := a.refreshPlane(r); err != nil {
		writeError(w, http.StatusServiceUnavailable, "provider_pool_refresh_failed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "revoked"})
}

// storeCredential encrypts and persists one credential payload.
func (a *API) storeCredential(r *http.Request, provider, accountID, purpose, mode string, payload map[string]any, expiresAt string) error {
	encrypted, err := a.Vault.Encrypt(payload)
	if err != nil {
		return err
	}
	write := store.CredentialWrite{
		Provider:         provider,
		AccountID:        accountID,
		Purpose:          purpose,
		Mode:             mode,
		EncryptedPayload: encrypted,
		HasRefreshToken:  payload["refresh_token"] != nil || provider == models.ProviderWorkBuddyIntl,
	}
	if expiresAt != "" {
		expires := expiresAt
		write.ExpiresAt = &expires
	}
	_, err = a.DB.UpsertCredential(r.Context(), write)
	return err
}

func (a *API) upsertChatPurpose(r *http.Request, provider, accountID, expiresAt string) error {
	purpose := store.Purpose{
		Provider:           provider,
		AccountID:          accountID,
		Purpose:            "chat",
		Enabled:            true,
		Status:             "active",
		VerificationStatus: "not_required",
		Capabilities:       []string{"chat"},
	}
	if expiresAt != "" {
		expires := expiresAt
		purpose.ExpiresAt = &expires
	}
	now := store.NowISO()
	purpose.LastSuccessAt = &now
	return a.DB.UpsertPurpose(r.Context(), purpose)
}

// ---- models ----

func (a *API) handleListModels(w http.ResponseWriter, r *http.Request) {
	query := r.URL.Query()
	catalog := a.catalogForRequest()
	providerFilter := query.Get("provider")
	search := strings.ToLower(strings.TrimSpace(query.Get("query")))
	enabledFilter := query.Get("enabled")
	capability := query.Get("capability")

	views := make([]map[string]any, 0, len(catalog))
	for _, entry := range catalog {
		routes := make([]map[string]any, 0, len(entry.Routes))
		for _, route := range entry.Routes {
			routes = append(routes, map[string]any{
				"provider": route.Provider, "upstream_id": route.UpstreamID, "enabled": true,
			})
		}
		if providerFilter != "" && !entryHasProvider(entry, providerFilter) {
			continue
		}
		if search != "" && !strings.Contains(strings.ToLower(entry.ID), search) {
			continue
		}
		view := map[string]any{
			"model_id":     entry.ID,
			"display_name": entry.Name,
			// Capability NAMES, not the capability flags: the console iterates
			// this list to render one tag per capability, so an object would be
			// iterated over its boolean values and display "true/false".
			"capabilities":     capabilitiesList(entry.Capabilities),
			"capability_flags": entry.Capabilities,
			"max_context":      entry.MaxContext,
			"max_output":       entry.MaxOutput,
			"enabled":          true,
			"routes":           routes,
		}
		if capability != "" && !hasCapability(entry.Capabilities, capability) {
			continue
		}
		if enabledFilter == "false" {
			continue
		}
		views = append(views, view)
	}
	sort.Slice(views, func(i, j int) bool {
		return views[i]["model_id"].(string) < views[j]["model_id"].(string)
	})
	limit, offset, err := pageFromRequest(r, 20)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	total := len(views)
	page := paginate(views, offset, limit)
	writeJSON(w, http.StatusOK, map[string]any{
		"models":      list(page),
		"next_cursor": nextCursor(offset, limit, len(page)),
		"total":       total,
	})
}

func entryHasProvider(entry models.Unified, provider string) bool {
	for _, route := range entry.Routes {
		if route.Provider == provider {
			return true
		}
	}
	return false
}

func hasCapability(capabilities models.Capabilities, name string) bool {
	switch name {
	case "chat":
		return capabilities.Chat
	case "streaming":
		return capabilities.Streaming
	case "tool_calling":
		return capabilities.ToolCalling
	case "reasoning":
		return capabilities.Reasoning
	case "reasoning_effort":
		return capabilities.ReasoningEffort
	case "context_window":
		return capabilities.ContextWindow
	case "max_output_tokens":
		return capabilities.MaxOutputTokens
	}
	return false
}

// handleRefreshModels writes the config definitions into the catalog table and
// reloads the runtime.
func (a *API) handleRefreshModels(w http.ResponseWriter, r *http.Request) {
	definitions := models.LoadDefinitions(a.Settings.ModelConfigPath)
	added, updated := 0, 0
	existing, err := a.DB.ListCatalogModels(r.Context(), models.KnownProviders)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	known := map[string]bool{}
	for _, model := range existing {
		known[model.Provider+":"+model.ModelID] = true
	}
	for provider, entries := range definitions {
		for _, definition := range entries {
			capabilities := capabilitiesList(definition.Capabilities)
			key := provider + ":" + definition.ID
			if known[key] {
				updated++
			} else {
				added++
			}
			if err := a.DB.UpsertCatalogModel(r.Context(), store.CatalogModel{
				Provider:     provider,
				ModelID:      definition.ID,
				DisplayName:  definition.Name,
				Capabilities: capabilities,
				Source:       "definition",
				Enabled:      true,
				Metadata:     definition.Metadata,
			}); err != nil {
				writeError(w, http.StatusInternalServerError, err.Error())
				return
			}
		}
	}
	a.audit(r, "model.refresh", "model", "", map[string]any{"added": added, "updated": updated})
	if err := a.refreshPlane(r); err != nil {
		writeError(w, http.StatusServiceUnavailable, "provider_pool_refresh_failed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"added": added, "updated": updated})
}

func capabilitiesList(capabilities models.Capabilities) []string {
	out := []string{}
	for name, enabled := range map[string]bool{
		"chat": capabilities.Chat, "streaming": capabilities.Streaming,
		"tool_calling": capabilities.ToolCalling, "reasoning": capabilities.Reasoning,
		"reasoning_effort": capabilities.ReasoningEffort, "context_window": capabilities.ContextWindow,
		"max_output_tokens": capabilities.MaxOutputTokens,
	} {
		if enabled {
			out = append(out, name)
		}
	}
	sort.Strings(out)
	return out
}

func (a *API) handlePatchModel(w http.ResponseWriter, r *http.Request) {
	modelID := r.PathValue("modelID")
	var body struct {
		Enabled *bool `json:"enabled"`
	}
	if err := decodeBody(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	entry, ok := a.catalogForRequest()[modelID]
	if !ok {
		writeError(w, http.StatusNotFound, "unknown model: "+modelID)
		return
	}
	enabled := true
	if body.Enabled != nil {
		enabled = *body.Enabled
	}
	affected := 0
	// There is one catalog row per provider route; flip every route of the model.
	for _, route := range entry.Routes {
		count, err := a.DB.SetCatalogModelEnabled(r.Context(), route.Provider, modelID, enabled)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		affected += count
	}
	a.audit(r, "model.update", "model", modelID, map[string]any{"enabled": enabled})
	if err := a.refreshPlane(r); err != nil {
		writeError(w, http.StatusServiceUnavailable, "provider_pool_refresh_failed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "updated", "model_id": modelID, "enabled": enabled, "routes": affected,
	})
}

func (a *API) handleProbeModel(w http.ResponseWriter, r *http.Request) {
	modelID := r.PathValue("modelID")
	entry, ok := a.catalogForRequest()[modelID]
	if !ok {
		writeError(w, http.StatusNotFound, "unknown model: "+modelID)
		return
	}
	results := make([]map[string]any, 0, len(entry.Routes))
	for _, route := range entry.Routes {
		results = append(results, a.probeRoute(r, route.Provider, modelID, route.UpstreamID))
	}
	status := "ok"
	for _, result := range results {
		if result["status"] != "ok" {
			status = "partial"
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status": status, "model_id": modelID, "routes": results,
	})
}

// ---- routing ----

// routeAccountView is one chat-capable account in the per-model block grid.
type routeAccountView struct {
	AccountID string     `json:"account_id"`
	Label     string     `json:"label"`
	Source    string     `json:"source"`
	Blocked   bool       `json:"blocked"`
	Block     *blockView `json:"block"`
}

// blockView describes why one account is excluded from one model.
type blockView struct {
	Source       string `json:"source"`
	Reason       string `json:"reason"`
	BlockedUntil string `json:"blocked_until"`
}

func (a *API) handleGetRouting(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	policyRows, err := a.DB.ListRoutePolicies(ctx)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	byModel := map[string]map[string]store.RoutePolicyRow{}
	configured := map[string]bool{}
	for _, row := range policyRows {
		if byModel[row.ModelID] == nil {
			byModel[row.ModelID] = map[string]store.RoutePolicyRow{}
		}
		byModel[row.ModelID][row.Provider] = row
		configured[row.ModelID] = true
	}

	accountsByProvider, err := a.chatAccountsByProvider(ctx)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	blocks, err := a.blockIndex(ctx)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}

	catalog := a.catalogForRequest()
	modelsList := make([]map[string]any, 0, len(catalog))
	for _, entry := range catalog {
		if len(entry.Routes) < 1 {
			continue
		}
		routes := make([]map[string]any, 0, len(entry.Routes))
		for _, route := range entry.Routes {
			// Defaults reproduce the historical behaviour: one tier, equal
			// weight, every route enabled.
			priority, weight, enabled := 0, 1, true
			if policy, ok := byModel[entry.ID][route.Provider]; ok {
				priority, weight, enabled = policy.Priority, policy.Weight, policy.Enabled
			}
			routes = append(routes, map[string]any{
				"provider":     route.Provider,
				"priority":     priority,
				"weight":       weight,
				"enabled":      enabled,
				"capabilities": capabilityNames(entry.Capabilities),
				"accounts":     list(routeAccounts(accountsByProvider[route.Provider], route.Provider, entry.ID, blocks)),
			})
		}
		modelsList = append(modelsList, map[string]any{
			"model_id":   entry.ID,
			"name":       entry.Name,
			"configured": configured[entry.ID],
			"routes":     routes,
		})
	}
	sort.Slice(modelsList, func(i, j int) bool {
		return modelsList[i]["model_id"].(string) < modelsList[j]["model_id"].(string)
	})
	writeJSON(w, http.StatusOK, map[string]any{
		"models":       list(modelsList),
		"quota_blocks": quotaBlocksJSON(a),
	})
}

// routeAccounts attaches block state to the chat-capable accounts of one provider.
func routeAccounts(accounts []routeAccountView, provider, modelID string, blocks map[[3]string]blockView) []routeAccountView {
	out := make([]routeAccountView, 0, len(accounts))
	for _, account := range accounts {
		account.Blocked = false
		account.Block = nil
		if block, ok := blocks[[3]string{provider, account.AccountID, modelID}]; ok {
			copied := block
			account.Blocked = true
			account.Block = &copied
		}
		out = append(out, account)
	}
	return out
}

// chatAccountsByProvider lists the accounts eligible for chat traffic, keyed by
// provider. This is the same population the proxy plane loads into its pools:
// an enabled account whose chat purpose is active.
func (a *API) chatAccountsByProvider(ctx context.Context) (map[string][]routeAccountView, error) {
	accounts, err := a.DB.ListAccounts(ctx, models.KnownProviders)
	if err != nil {
		return nil, err
	}
	purposes, err := a.DB.ListAllPurposes(ctx)
	if err != nil {
		return nil, err
	}
	active := map[string]bool{}
	for _, purpose := range purposes {
		if purpose.Purpose == "chat" && purpose.Enabled && purpose.Status == "active" {
			active[purpose.Provider+":"+purpose.AccountID] = true
		}
	}
	grouped := map[string][]routeAccountView{}
	for _, account := range accounts {
		if !account.Enabled {
			continue
		}
		if !models.ChatOnlyProviders[account.Provider] && !active[account.Provider+":"+account.AccountID] {
			continue
		}
		label := account.Label
		if label == "" {
			label = account.AccountID
		}
		grouped[account.Provider] = append(grouped[account.Provider], routeAccountView{
			AccountID: account.AccountID,
			Label:     label,
			Source:    account.Source,
		})
	}
	return grouped, nil
}

// blockIndex maps (provider, account, model) to block info, merging stored
// manual exclusions with the live auto-detected ones. Stored rows win: a manual
// block stays authoritative even before the pool picks up the new snapshot.
func (a *API) blockIndex(ctx context.Context) (map[[3]string]blockView, error) {
	index := map[[3]string]blockView{}
	rows, err := a.DB.ListAccountModelBlocks(ctx)
	if err != nil {
		return nil, err
	}
	for _, row := range rows {
		source := row.Source
		if source == "" {
			source = "manual"
		}
		index[[3]string{row.Provider, row.AccountID, row.ModelID}] = blockView{
			Source:       source,
			Reason:       row.Reason,
			BlockedUntil: derefString(row.BlockedUntil),
		}
	}
	if a.Plane == nil {
		return index, nil
	}
	for _, block := range a.Plane.QuotaBlocks() {
		if block.Provider == "" || block.AccountID == "" || block.ModelID == "" {
			continue
		}
		key := [3]string{block.Provider, block.AccountID, block.ModelID}
		if _, stored := index[key]; stored {
			continue
		}
		source := block.Source
		if source == "" {
			source = "auto"
		}
		index[key] = blockView{Source: source, Reason: block.Reason, BlockedUntil: block.BlockedUntil}
	}
	return index, nil
}

// capabilityNames renders the capability flags the way the console expects
// them: a list of enabled names, not a map of booleans.
func capabilityNames(capabilities models.Capabilities) []string {
	ordered := []struct {
		name    string
		enabled bool
	}{
		{"chat", capabilities.Chat},
		{"streaming", capabilities.Streaming},
		{"tool_calling", capabilities.ToolCalling},
		{"reasoning", capabilities.Reasoning},
		{"reasoning_effort", capabilities.ReasoningEffort},
		{"context_window", capabilities.ContextWindow},
		{"max_output_tokens", capabilities.MaxOutputTokens},
	}
	names := []string{}
	for _, item := range ordered {
		if item.enabled {
			names = append(names, item.name)
		}
	}
	return names
}

func quotaBlocksJSON(a *API) []providers.QuotaBlock {
	if a.Plane == nil {
		return []providers.QuotaBlock{}
	}
	return a.Plane.QuotaBlocks()
}

func (a *API) handlePutRouting(w http.ResponseWriter, r *http.Request) {
	modelID := r.PathValue("modelID")
	entry, ok := a.catalogForRequest()[modelID]
	if !ok {
		writeError(w, http.StatusNotFound, "unknown model: "+modelID)
		return
	}
	var body struct {
		Routes []struct {
			Provider string `json:"provider"`
			Priority *int   `json:"priority"`
			Weight   *int   `json:"weight"`
			Enabled  *bool  `json:"enabled"`
		} `json:"routes"`
	}
	if err := decodeBody(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	known := map[string]bool{}
	for _, route := range entry.Routes {
		known[route.Provider] = true
	}
	rows := make([]store.RoutePolicyRow, 0, len(body.Routes))
	anyEnabled := false
	positiveWeight := false
	for _, submitted := range body.Routes {
		if !known[submitted.Provider] {
			writeError(w, http.StatusBadRequest, "unknown route provider: "+submitted.Provider)
			return
		}
		priority, weight, enabled := 0, 1, true
		if submitted.Priority != nil {
			priority = *submitted.Priority
		}
		if submitted.Weight != nil {
			weight = *submitted.Weight
		}
		if submitted.Enabled != nil {
			enabled = *submitted.Enabled
		}
		if priority < 0 || priority > providers.MaxPriority || weight < 0 || weight > providers.MaxWeight {
			writeError(w, http.StatusBadRequest, "route policy out of range")
			return
		}
		if enabled {
			anyEnabled = true
			if weight > 0 {
				positiveWeight = true
			}
		}
		rows = append(rows, store.RoutePolicyRow{
			ModelID: modelID, Provider: submitted.Provider,
			Priority: priority, Weight: weight, Enabled: enabled,
		})
	}
	if !anyEnabled {
		writeError(w, http.StatusBadRequest, "at least one route must stay enabled")
		return
	}
	if !positiveWeight {
		writeError(w, http.StatusBadRequest, "at least one enabled route needs a positive weight")
		return
	}
	if err := a.DB.ReplaceRoutePolicies(r.Context(), modelID, rows); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	a.audit(r, "routing.update", "routing", modelID, nil)
	if err := a.refreshPlane(r); err != nil {
		writeError(w, http.StatusServiceUnavailable, "provider_pool_refresh_failed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "updated", "model_id": modelID})
}

func (a *API) handleDeleteRouting(w http.ResponseWriter, r *http.Request) {
	modelID := r.PathValue("modelID")
	if err := a.DB.DeleteRoutePolicies(r.Context(), modelID); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	a.audit(r, "routing.delete", "routing", modelID, nil)
	if err := a.refreshPlane(r); err != nil {
		writeError(w, http.StatusServiceUnavailable, "provider_pool_refresh_failed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "deleted", "model_id": modelID})
}

// ---- proxy keys ----

func (a *API) handleListProxyKeys(w http.ResponseWriter, r *http.Request) {
	keys, err := a.DB.ListProxyKeys(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	views := make([]map[string]any, 0, len(keys))
	for _, key := range keys {
		views = append(views, map[string]any{
			"key_id": key.KeyID, "name": key.Name, "scopes": key.Scopes,
			"enabled": key.Enabled, "created_at": key.CreatedAt,
			"last_used_at": key.LastUsedAt, "expires_at": key.ExpiresAt,
			"revoked_at": key.RevokedAt,
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"keys": list(views)})
}

func (a *API) handleCreateProxyKey(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Name      string   `json:"name"`
		Scopes    []string `json:"scopes"`
		ExpiresAt string   `json:"expires_at"`
	}
	_ = decodeBody(r, &body)
	name := body.Name
	if name == "" {
		name = "proxy-key"
	}
	raw := "qb2api_" + randomToken(24)
	key := store.ProxyKey{
		KeyID:     "pk_" + randomToken(8),
		Name:      name,
		KeyHash:   hashToken(raw),
		Scopes:    body.Scopes,
		Enabled:   true,
		CreatedAt: store.NowISO(),
	}
	if body.ExpiresAt != "" {
		expires := body.ExpiresAt
		key.ExpiresAt = &expires
	}
	if err := a.DB.CreateProxyKey(r.Context(), key); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	a.audit(r, "proxy_key.create", "proxy_key", key.KeyID, nil)
	if err := a.refreshPlane(r); err != nil {
		writeError(w, http.StatusServiceUnavailable, "provider_pool_refresh_failed")
		return
	}
	// The raw key is returned exactly once; only its digest is stored.
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "created", "key_id": key.KeyID, "name": key.Name,
		"key": raw, "created_at": key.CreatedAt, "expires_at": key.ExpiresAt,
	})
}

func (a *API) handleRevokeProxyKey(w http.ResponseWriter, r *http.Request) {
	keyID := r.PathValue("keyID")
	if err := a.DB.RevokeProxyKey(r.Context(), keyID); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	a.audit(r, "proxy_key.revoke", "proxy_key", keyID, nil)
	if err := a.refreshPlane(r); err != nil {
		writeError(w, http.StatusServiceUnavailable, "provider_pool_refresh_failed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "revoked", "key_id": keyID})
}

// refreshPlane rebuilds the credential slots, catalog and router after a
// storage mutation.
//
// Callers MUST check the error before writing a success response: a failed
// rebuild means the mutation is stored but not yet live, and reporting success
// would make the console show a state the proxy is not serving. That is the
// same contract as the Python refresh_after_mutation, which surfaced a 503.
func (a *API) refreshPlane(r *http.Request) error {
	if a.Plane == nil {
		return nil
	}
	ctx, cancel := ctxWithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	return a.Plane.Reload(ctx, a.DB)
}
