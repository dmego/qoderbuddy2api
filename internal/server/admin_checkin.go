package server

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"github.com/dmego/qoderbuddy2api/internal/checkin"
	"github.com/dmego/qoderbuddy2api/internal/store"
)

// lowerOutcome renders a stored outcome the way the console compares it.
func lowerOutcome(value *string) any {
	if value == nil {
		return nil
	}
	return strings.ToLower(*value)
}

// registerCheckin installs the sign-in routes.
//
// Paths and response shapes are pinned by frontend/src/pages/CheckinPage.vue and
// AccountDetailPage.vue.
func (a *API) registerCheckin(mux *http.ServeMux, guard func(http.HandlerFunc) http.HandlerFunc) {
	mux.HandleFunc("GET /api/admin/checkin/status", guard(a.handleCheckinStatus))
	mux.HandleFunc("POST /api/admin/checkin/run", guard(a.handleCheckinRun))
	mux.HandleFunc("GET /api/admin/checkin/runs", guard(a.handleCheckinRuns))
	mux.HandleFunc("GET /api/admin/checkin/runs/{runID}", guard(a.handleCheckinRunDetail))
}

func (a *API) handleCheckinStatus(w http.ResponseWriter, r *http.Request) {
	if a.Checkin == nil {
		status := fallbackCheckinStatus()
		status["scheduler"] = a.checkinSchedulerStatus()
		status["metrics"] = a.metricsSchedulerStatus()
		writeJSON(w, http.StatusOK, status)
		return
	}
	nextRunAt := ""
	if a.CheckinScheduler != nil {
		if value, ok := a.CheckinScheduler.Status()["next_run_at"].(string); ok {
			nextRunAt = value
		}
	}
	status, err := a.Checkin.Status(r.Context(), nextRunAt)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	// The scheduler block is a separate read so a failing scheduler cannot take
	// the whole status page down with it.
	status["scheduler"] = a.checkinSchedulerStatus()
	status["metrics"] = a.metricsSchedulerStatus()
	writeJSON(w, http.StatusOK, status)
}

// checkinSchedulerStatus renders the scheduler snapshot, or a disabled block.
func (a *API) checkinSchedulerStatus() map[string]any {
	if a.CheckinScheduler == nil {
		return map[string]any{
			"catch_up_decision": nil, "active_run_id": nil,
			"last_error": nil, "last_run_at": nil, "next_run_at": nil,
		}
	}
	return a.CheckinScheduler.Status()
}

// metricsSchedulerStatus renders the metrics scheduler snapshot.
func (a *API) metricsSchedulerStatus() map[string]any {
	if a.MetricsScheduler == nil {
		return map[string]any{
			"enabled": false, "running": false, "refresh_in_progress": false,
			"last_error": nil, "backoff": []any{},
		}
	}
	return a.MetricsScheduler.Status()
}

// fallbackCheckinStatus is served while the sign-in subsystem is unavailable, so
// the console renders an empty page instead of an error.
func fallbackCheckinStatus() map[string]any {
	return map[string]any{
		"enabled":           false,
		"running":           false,
		"local_date":        "",
		"timezone":          "",
		"checkin_at":        "",
		"next_run_at":       nil,
		"active_run_id":     nil,
		"eligible_accounts": []any{},
		"daily_states":      []any{},
	}
}

func (a *API) handleCheckinRun(w http.ResponseWriter, r *http.Request) {
	if a.Checkin == nil {
		writeError(w, http.StatusServiceUnavailable, "checkin unavailable")
		return
	}
	var body struct {
		Targets []struct {
			Provider  string `json:"provider"`
			AccountID string `json:"account_id"`
		} `json:"targets"`
	}
	_ = decodeBody(r, &body)
	targets := make([]checkin.AccountRef, 0, len(body.Targets))
	for _, target := range body.Targets {
		if target.AccountID == "" {
			continue
		}
		provider := target.Provider
		if provider == "" {
			provider = "codebuddy"
		}
		targets = append(targets, checkin.AccountRef{Provider: provider, AccountID: target.AccountID})
	}

	runID, err := a.Checkin.StartBatch(r.Context(), "manual", targets, false)
	if err != nil {
		if errors.Is(err, checkin.ErrRunInProgress) {
			writeError(w, http.StatusConflict, "checkin_run_in_progress")
			return
		}
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	a.audit(r, "checkin.run", "checkin", runID, map[string]any{"targets": len(targets)})
	// The console polls the run history, so the id is the useful answer.
	writeJSON(w, http.StatusAccepted, map[string]any{
		"status":       "running",
		"operation_id": runID,
		"run_id":       runID,
	})
}

func (a *API) handleCheckinRuns(w http.ResponseWriter, r *http.Request) {
	limit, offset, err := pageFromRequest(r, 20)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	query := r.URL.Query()
	status := query.Get("status")
	// The console labels the scheduler trigger "scheduled"; the database stores
	// it as "scheduler". Passing the label straight through silently matches
	// nothing and shows an empty history.
	trigger := query.Get("trigger")
	if trigger == "scheduled" {
		trigger = "scheduler"
	}
	runs, err := a.DB.ListCheckinRuns(r.Context(), status, trigger, limit, offset)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	total, err := a.DB.CountCheckinRuns(r.Context(), status, trigger)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	views := make([]map[string]any, 0, len(runs))
	for _, run := range runs {
		views = append(views, a.checkinRunView(r, run))
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"runs":        views,
		"next_cursor": nextCursor(offset, limit, len(views)),
		"total":       total,
		"limit":       limit,
	})
}

func (a *API) handleCheckinRunDetail(w http.ResponseWriter, r *http.Request) {
	runID := r.PathValue("runID")
	run, err := a.DB.GetCheckinRun(r.Context(), runID)
	if err != nil {
		writeError(w, http.StatusNotFound, "run_not_found")
		return
	}
	attempts, err := a.DB.ListCheckinAttempts(r.Context(), runID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	views := make([]map[string]any, 0, len(attempts))
	for _, attempt := range attempts {
		views = append(views, checkinAttemptView(attempt))
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"run":      a.checkinRunView(r, run),
		"attempts": views,
	})
}

// checkinRunView adds the per-run aggregate counters the history table shows.
//
// successful_count counts SKIPPED as well as the two claim outcomes: an account
// that was already terminal today is a successful batch outcome, and excluding
// it made a healthy catch-up run display as "0 successful".
func (a *API) checkinRunView(r *http.Request, run store.CheckinRun) map[string]any {
	view := map[string]any{
		"run_id":           run.RunID,
		"started_at":       run.StartedAt,
		"finished_at":      run.FinishedAt,
		"status":           run.Status,
		"trigger":          run.Trigger,
		"local_date":       run.LocalDate,
		"timezone":         run.Timezone,
		"attempt_count":    0,
		"successful_count": 0,
	}
	attempts, err := a.DB.ListCheckinAttempts(r.Context(), run.RunID)
	if err != nil {
		return view
	}
	successes := 0
	for _, attempt := range attempts {
		if attempt.Outcome == nil {
			continue
		}
		switch *attempt.Outcome {
		case checkin.OutcomeClaimed, checkin.OutcomeAlreadyCheckedIn, checkin.OutcomeSkipped:
			successes++
		}
	}
	view["attempt_count"] = len(attempts)
	view["successful_count"] = successes
	return view
}

// checkinAttemptView renders one attempt for the console.
//
// The outcome is lower-cased because the console compares it against lowercase
// literals ("claimed", "already_checked_in", "needs_reauth"); the uppercase
// value is what the database stores, not what the API returns. Mismatching this
// silently makes every successful check-in render as a failure.
//
// Reward and quota fields are only populated for the two claim outcomes: a
// failed or skipped attempt has no reward, and echoing the previous run's
// numbers would look like a fresh credit.
func checkinAttemptView(attempt store.CheckinAttempt) map[string]any {
	outcome := ""
	if attempt.Outcome != nil {
		outcome = strings.ToLower(*attempt.Outcome)
	}
	rewardVisible := outcome == "claimed" || outcome == "already_checked_in"
	view := map[string]any{
		"provider":    attempt.Provider,
		"account_id":  attempt.AccountID,
		"outcome":     outcome,
		"http_status": attempt.HTTPStatus,
		"attempts":    attempt.Attempts,
		"finished_at": attempt.FinishedAt,
		"error_code":  checkinErrorCode(attempt),
	}
	for _, key := range []string{
		"reward_credits", "reward_expires_at", "quota_before",
		"quota_after", "quota_delta", "quota_observed_at", "quota_change_status",
	} {
		view[key] = nil
	}
	if !rewardVisible {
		return view
	}
	view["reward_credits"] = attempt.RewardCredits
	view["reward_expires_at"] = attempt.RewardExpiresAt
	view["quota_change_status"] = attempt.QuotaChangeStatus
	view["quota_observed_at"] = attempt.QuotaObservedAt
	if attempt.QuotaBeforeJSON != nil {
		view["quota_before"] = decodeJSONField(*attempt.QuotaBeforeJSON)
	}
	if attempt.QuotaAfterJSON != nil {
		view["quota_after"] = decodeJSONField(*attempt.QuotaAfterJSON)
	}
	if attempt.QuotaDeltaJSON != nil {
		view["quota_delta"] = decodeJSONField(*attempt.QuotaDeltaJSON)
	}
	return view
}

// checkinErrorCode reports the upstream business code when there is one, and a
// generic marker when the attempt failed for a reason that has no code.
func checkinErrorCode(attempt store.CheckinAttempt) any {
	if attempt.BusinessCode != nil {
		return *attempt.BusinessCode
	}
	if attempt.RedactedError != nil && *attempt.RedactedError != "" {
		return "checkin_failed"
	}
	return nil
}

func decodeJSONField(raw string) any {
	var out any
	if err := json.Unmarshal([]byte(raw), &out); err != nil {
		return nil
	}
	return out
}
