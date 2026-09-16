package server

import (
	"errors"
	"net/http"
	"strings"

	"github.com/dmego/qoderbuddy2api/internal/models"
	"github.com/dmego/qoderbuddy2api/internal/oauthflow"
)

// registerImports installs the browser-login import routes.
//
// The console opens the provider's own login page and polls for completion, so
// start and poll are separate calls and poll is a POST (the console sends the
// flow id in the body). Paths and response shapes are pinned by
// frontend/src/components/AccountImportPanel.vue.
func (a *API) registerImports(mux *http.ServeMux, guard func(http.HandlerFunc) http.HandlerFunc) {
	mux.HandleFunc("POST /api/admin/auth/{provider}/start", guard(a.handleImportStart))
	mux.HandleFunc("POST /api/admin/auth/{provider}/poll", guard(a.handleImportPoll))
	mux.HandleFunc("POST /api/admin/auth/{provider}/manual", guard(a.handleImportManual))
	mux.HandleFunc("POST /api/admin/auth/{provider}/checkin", guard(a.handleImportCheckin))
}

// importProvider normalizes the path parameter. The console uses a hyphenated
// form for the international deployment ("workbuddy-intl") while the database
// uses an underscore, so both must resolve to the stored provider id.
func importProvider(raw string) (string, bool) {
	switch strings.ReplaceAll(raw, "-", "_") {
	case models.ProviderWorkBuddy, "workbuddy":
		return models.ProviderWorkBuddy, true
	case models.ProviderWorkBuddyIntl:
		return models.ProviderWorkBuddyIntl, true
	}
	return "", false
}

// importResult is the shape the console reads.
type importResult struct {
	Account         *accountReference `json:"account,omitempty"`
	CheckinDerived  bool              `json:"checkin_derived,omitempty"`
	CheckinVerified bool              `json:"checkin_verified,omitempty"`
}

type accountReference struct {
	Provider  string `json:"provider"`
	AccountID string `json:"account_id"`
	Label     string `json:"label"`
}

func (a *API) handleImportStart(w http.ResponseWriter, r *http.Request) {
	provider, ok := importProvider(r.PathValue("provider"))
	if !ok {
		writeError(w, http.StatusBadRequest, "unsupported_provider")
		return
	}
	if a.Imports == nil {
		writeError(w, http.StatusServiceUnavailable, "import service unavailable")
		return
	}
	var body struct {
		Label     string `json:"label"`
		AccountID string `json:"account_id"`
	}
	_ = decodeBody(r, &body)
	flow, err := a.Imports.Start(r.Context(), provider, body.Label)
	if err != nil {
		// The upstream state endpoint may be unreachable from a restricted
		// network; the manual import path exists for exactly that case, so the
		// error names it rather than leaving the operator stuck.
		writeError(w, http.StatusBadGateway, "登录流程创建失败："+err.Error()+"；可使用下方「手动输入 Bearer Token」导入")
		return
	}
	a.audit(r, "import.start", "account", provider, nil)
	writeJSON(w, http.StatusOK, flow)
}

func (a *API) handleImportPoll(w http.ResponseWriter, r *http.Request) {
	provider, ok := importProvider(r.PathValue("provider"))
	if !ok {
		writeError(w, http.StatusBadRequest, "unsupported_provider")
		return
	}
	if a.Imports == nil {
		writeError(w, http.StatusServiceUnavailable, "import service unavailable")
		return
	}
	var body struct {
		FlowID string `json:"flow_id"`
	}
	_ = decodeBody(r, &body)
	flow, err := a.Imports.Poll(r.Context(), provider, body.FlowID)
	if err != nil {
		if errors.Is(err, oauthflow.ErrNotFound) {
			writeError(w, http.StatusNotFound, "flow_not_found")
			return
		}
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	response := map[string]any{
		"status":     flow.Status,
		"expires_at": flow.ExpiresAt,
		"flow_id":    flow.FlowID,
	}
	if flow.Status == "success" && flow.AccountID != "" {
		response["account"] = accountReference{
			Provider:  flow.Provider,
			AccountID: flow.AccountID,
			Label:     flow.Label,
		}
		// The domestic deployment has a sign-in centre; the international one is
		// chat-only, so no sign-in purpose is implied there.
		if flow.Provider == models.ProviderWorkBuddy {
			response["checkin_verified"] = false
		}
		a.audit(r, "import.complete", "account", flow.Provider+"/"+flow.AccountID, nil)
		a.refreshPlane(r)
	} else if flow.Message != "" {
		response["message"] = flow.Message
	}
	writeJSON(w, http.StatusOK, response)
}

func (a *API) handleImportManual(w http.ResponseWriter, r *http.Request) {
	provider, ok := importProvider(r.PathValue("provider"))
	if !ok {
		writeError(w, http.StatusBadRequest, "unsupported_provider")
		return
	}
	if a.Imports == nil {
		writeError(w, http.StatusServiceUnavailable, "import service unavailable")
		return
	}
	var body struct {
		Label        string `json:"label"`
		AccountID    string `json:"account_id"`
		Token        string `json:"token"`
		AccessToken  string `json:"access_token"`
		RefreshToken string `json:"refresh_token"`
		ExpiresAt    string `json:"expires_at"`
	}
	_ = decodeBody(r, &body)
	token := firstNonEmptyString(body.Token, body.AccessToken)
	if token == "" {
		writeError(w, http.StatusBadRequest, "token_required")
		return
	}
	payload := map[string]any{"access_token": token}
	if body.RefreshToken != "" {
		payload["refresh_token"] = body.RefreshToken
	}
	flow, err := a.Imports.Manual(r.Context(), provider, body.Label, payload, body.ExpiresAt)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	a.audit(r, "import.manual", "account", provider+"/"+flow.AccountID, nil)
	if err := a.refreshPlane(r); err != nil {
		writeError(w, http.StatusServiceUnavailable, "provider_pool_refresh_failed")
		return
	}
	writeJSON(w, http.StatusOK, importResult{
		Account: &accountReference{Provider: provider, AccountID: flow.AccountID, Label: flow.Label},
	})
}

// handleImportCheckin imports a sign-in credential for the domestic deployment.
func (a *API) handleImportCheckin(w http.ResponseWriter, r *http.Request) {
	provider, ok := importProvider(r.PathValue("provider"))
	if !ok {
		writeError(w, http.StatusBadRequest, "unsupported_provider")
		return
	}
	if provider != models.ProviderWorkBuddy {
		// The international deployment has no sign-in centre.
		writeError(w, http.StatusBadRequest, "unsupported_provider")
		return
	}
	if a.Imports == nil {
		writeError(w, http.StatusServiceUnavailable, "import service unavailable")
		return
	}
	var body struct {
		AccountID   string `json:"account_id"`
		Mode        string `json:"mode"`
		Token       string `json:"token"`
		AccessToken string `json:"access_token"`
		Cookie      string `json:"cookie"`
		ExpiresAt   string `json:"expires_at"`
	}
	_ = decodeBody(r, &body)
	if body.AccountID == "" {
		writeError(w, http.StatusBadRequest, "account_id_required")
		return
	}
	token := firstNonEmptyString(body.Token, body.AccessToken)
	if token == "" && body.Cookie == "" {
		writeError(w, http.StatusBadRequest, "credential_required")
		return
	}
	payload := map[string]any{}
	if token != "" {
		payload["access_token"] = token
	}
	if body.Cookie != "" {
		payload["cookie"] = body.Cookie
	}
	mode := body.Mode
	if mode == "" {
		mode = "bearer"
	}
	flow, err := a.Imports.ManualCheckin(r.Context(), provider, body.AccountID, mode, payload, body.ExpiresAt)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	a.audit(r, "import.checkin", "account", provider+"/"+body.AccountID, nil)
	if err := a.refreshPlane(r); err != nil {
		writeError(w, http.StatusServiceUnavailable, "provider_pool_refresh_failed")
		return
	}
	writeJSON(w, http.StatusOK, importResult{
		Account:         &accountReference{Provider: provider, AccountID: flow.AccountID},
		CheckinVerified: true,
	})
}

func firstNonEmptyString(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}
