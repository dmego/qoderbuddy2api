package server

import (
	"errors"
	"net/http"

	"github.com/dmego/qoderbuddy2api/internal/growth"
)

// registerGrowth installs the growth-centre routes. They are the live read plus
// the three mutation entry points the growth page drives.
func (a *API) registerGrowth(mux *http.ServeMux, guard func(http.HandlerFunc) http.HandlerFunc) {
	mux.HandleFunc("GET /api/admin/accounts/{provider}/{accountID}/growth", guard(a.handleGrowthOverview))
	mux.HandleFunc("POST /api/admin/accounts/{provider}/{accountID}/growth/execute", guard(a.handleGrowthExecute))
	mux.HandleFunc("POST /api/admin/accounts/{provider}/{accountID}/growth/run/{step}", guard(a.handleGrowthRun))
	mux.HandleFunc("GET /api/admin/accounts/{provider}/{accountID}/growth/history", guard(a.handleGrowthHistory))
	mux.HandleFunc("POST /api/admin/accounts/{provider}/{accountID}/growth/active-day/rerun", guard(a.handleGrowthRerunActiveDay))
}

// growthAutomation returns the automation, or an error the handler renders as
// 503 so a disabled subsystem does not look like a bad request.
func (a *API) growthAutomation() (*growth.Automation, error) {
	if a.Growth == nil {
		return nil, errors.New("growth automation unavailable")
	}
	return a.Growth, nil
}

// writeGrowthError maps the automation's typed errors onto HTTP responses. The
// codes are the ones the console already matches on.
func writeGrowthError(w http.ResponseWriter, err error) {
	var automationErr *growth.AutomationError
	if errors.As(err, &automationErr) {
		writeError(w, automationErr.Status, automationErr.Code)
		return
	}
	writeError(w, http.StatusInternalServerError, err.Error())
}

func (a *API) handleGrowthOverview(w http.ResponseWriter, r *http.Request) {
	automation, err := a.growthAutomation()
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	payload, err := automation.Overview(r.Context(), r.PathValue("provider"), r.PathValue("accountID"))
	if err != nil {
		writeGrowthError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, payload)
}

func (a *API) handleGrowthExecute(w http.ResponseWriter, r *http.Request) {
	automation, err := a.growthAutomation()
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	provider := r.PathValue("provider")
	accountID := r.PathValue("accountID")
	results, err := automation.RunAccount(r.Context(), provider, accountID, "manual")
	if err != nil {
		writeGrowthError(w, err)
		return
	}
	a.audit(r, "account.growth_execute", "account", provider+":"+accountID, nil)
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "ok",
		"result": results,
	})
}

func (a *API) handleGrowthRun(w http.ResponseWriter, r *http.Request) {
	automation, err := a.growthAutomation()
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	provider := r.PathValue("provider")
	accountID := r.PathValue("accountID")
	step := r.PathValue("step")
	result, err := automation.RunStep(r.Context(), provider, accountID, step)
	if err != nil {
		writeGrowthError(w, err)
		return
	}
	// An unknown step is logged like any other single-step run: the console
	// shows the failure detail rather than an HTTP error.
	if err := automation.RecordRun(r.Context(), provider, accountID, "manual:"+step,
		map[string]growth.StepResult{step: result}); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	automation.MaybeRefreshMetrics(r.Context(), provider, accountID,
		map[string]growth.StepResult{step: result})
	a.audit(r, "account.growth_step", "account", provider+":"+accountID,
		map[string]any{"step": step})
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "ok",
		"step":   step,
		"result": result.Map(),
	})
}

func (a *API) handleGrowthRerunActiveDay(w http.ResponseWriter, r *http.Request) {
	automation, err := a.growthAutomation()
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	provider := r.PathValue("provider")
	accountID := r.PathValue("accountID")
	result, err := automation.RunActiveDay(r.Context(), provider, accountID, true)
	if err != nil {
		writeGrowthError(w, err)
		return
	}
	// The rerun does not refresh metrics: it earns no credits, it only spends a
	// chat turn to light the day.
	if err := automation.RecordRun(r.Context(), provider, accountID, "manual:active_day_rerun",
		map[string]growth.StepResult{growth.StepActiveDay: result}); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	a.audit(r, "account.growth_active_day_rerun", "account", provider+":"+accountID, nil)
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "ok",
		"step":   growth.StepActiveDay,
		"result": result.Map(),
	})
}

func (a *API) handleGrowthHistory(w http.ResponseWriter, r *http.Request) {
	provider := r.PathValue("provider")
	accountID := r.PathValue("accountID")
	if _, err := a.DB.GetAccount(r.Context(), provider, accountID); err != nil {
		writeError(w, http.StatusNotFound, "account_not_found")
		return
	}
	// The console pages with page/page_size rather than an opaque cursor, and
	// its "latest" query sends page_size alone.
	page := 1
	if parsed, ok := numeric(r.URL.Query().Get("page")); ok && parsed >= 1 {
		page = parsed
	}
	pageSize := 10
	if parsed, ok := numeric(r.URL.Query().Get("page_size")); ok && parsed >= 1 {
		pageSize = parsed
	}
	if pageSize > 100 {
		pageSize = 100
	}
	total, err := a.DB.CountGrowthLog(r.Context(), provider, accountID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	logs, err := a.DB.ListGrowthLog(r.Context(), provider, accountID, pageSize, (page-1)*pageSize)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	pages := 0
	if total > 0 {
		pages = (total + pageSize - 1) / pageSize
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"logs":      logs,
		"total":     total,
		"page":      page,
		"page_size": pageSize,
		"pages":     pages,
	})
}
