package server

import (
	"errors"
	"net/http"
	"time"

	"github.com/dmego/qoderbuddy2api/gobackend/internal/store"
)

// registerAdmin installs the admin console API.
func (a *API) registerAdmin(mux *http.ServeMux) {
	guard := a.Admin.requireAdmin

	// Session bootstrap is public: it is the login endpoint.
	mux.HandleFunc("POST /api/admin/session", a.handleLogin)
	mux.HandleFunc("GET /api/admin/session", a.handleSessionInfo)
	mux.HandleFunc("POST /api/admin/session/logout", a.handleLogout)

	mux.HandleFunc("GET /api/admin/accounts", guard(a.handleListAccounts))
	mux.HandleFunc("POST /api/admin/accounts", guard(a.handleCreateAccount))
	mux.HandleFunc("GET /api/admin/accounts/{provider}/{accountID}", guard(a.handleGetAccount))
	mux.HandleFunc("PATCH /api/admin/accounts/{provider}/{accountID}", guard(a.handlePatchAccount))
	mux.HandleFunc("DELETE /api/admin/accounts/{provider}/{accountID}", guard(a.handleDeleteAccount))
	mux.HandleFunc("POST /api/admin/accounts/{provider}/{accountID}/refresh", guard(a.handleRefreshAccount))
	mux.HandleFunc("POST /api/admin/accounts/{provider}/{accountID}/probe", guard(a.handleProbeAccount))
	mux.HandleFunc("POST /api/admin/accounts/{provider}/{accountID}/promote", guard(a.handlePromoteAccount))
	mux.HandleFunc("POST /api/admin/accounts/{provider}/{accountID}/verify-checkin", guard(a.handleVerifyCheckin))
	mux.HandleFunc("POST /api/admin/accounts/{provider}/{accountID}/rederive-checkin", guard(a.handleRederiveCheckin))

	mux.HandleFunc("GET /api/admin/credentials", guard(a.handleListCredentials))
	mux.HandleFunc("POST /api/admin/credentials/{provider}/{accountID}/{purpose}/rotate", guard(a.handleRotateCredential))
	mux.HandleFunc("POST /api/admin/credentials/{provider}/{accountID}/{purpose}/revoke", guard(a.handleRevokeCredential))

	mux.HandleFunc("GET /api/admin/models", guard(a.handleListModels))
	mux.HandleFunc("POST /api/admin/models/refresh", guard(a.handleRefreshModels))
	mux.HandleFunc("PATCH /api/admin/models/{modelID}", guard(a.handlePatchModel))
	mux.HandleFunc("POST /api/admin/models/{modelID}/probe", guard(a.handleProbeModel))

	mux.HandleFunc("GET /api/admin/routing", guard(a.handleGetRouting))
	mux.HandleFunc("PUT /api/admin/routing/{modelID}", guard(a.handlePutRouting))
	mux.HandleFunc("DELETE /api/admin/routing/{modelID}", guard(a.handleDeleteRouting))

	mux.HandleFunc("GET /api/admin/proxy-keys", guard(a.handleListProxyKeys))
	mux.HandleFunc("POST /api/admin/proxy-keys", guard(a.handleCreateProxyKey))
	mux.HandleFunc("DELETE /api/admin/proxy-keys/{keyID}", guard(a.handleRevokeProxyKey))

	mux.HandleFunc("GET /api/admin/settings", guard(a.handleGetSettings))
	mux.HandleFunc("PATCH /api/admin/settings", guard(a.handlePatchSettings))

	mux.HandleFunc("GET /api/admin/audit", guard(a.handleListAudit))
	mux.HandleFunc("GET /api/admin/backup", guard(a.handleListBackups))
	mux.HandleFunc("POST /api/admin/backup", guard(a.handleCreateBackup))

	mux.HandleFunc("GET /api/admin/service", guard(a.handleServiceStatus))
	mux.HandleFunc("POST /api/admin/service/{action}", guard(a.handleServiceAction))
	mux.HandleFunc("GET /api/admin/service/events", guard(a.handleServiceEvents))
	mux.HandleFunc("GET /api/admin/service/operations/{operationID}", guard(a.handleServiceOperation))

	a.registerUsage(mux, guard)
	a.registerMetrics(mux, guard)
	a.registerCheckin(mux, guard)
	a.registerGrowth(mux, guard)
	a.registerImports(mux, guard)
}

// ---- session ----

func (a *API) handleLogin(w http.ResponseWriter, r *http.Request) {
	var body struct {
		AdminKey string `json:"admin_key"`
	}
	_ = decodeBody(r, &body)
	if body.AdminKey == "" {
		body.AdminKey = bearerToken(r)
	}
	token, csrf, err := a.Admin.Login(r.Context(), r, body.AdminKey)
	switch {
	case errors.Is(err, errRateLimited):
		writeError(w, http.StatusTooManyRequests, "login_rate_limited")
		return
	case errors.Is(err, errUnauthorized):
		writeError(w, http.StatusUnauthorized, "invalid_admin_key")
		return
	case err != nil:
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if err := a.Admin.SetSessionCookie(w, r, token); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status":     "authenticated",
		"csrf_token": csrf,
		"expires_in": maxInt(a.Settings.AdminSessionTTL, 1) * 3600,
	})
}

func (a *API) handleSessionInfo(w http.ResponseWriter, r *http.Request) {
	if err := a.Admin.VerifyRequest(r); err != nil {
		writeError(w, http.StatusUnauthorized, "invalid_admin_key")
		return
	}
	// A bearer-key caller has no cookie session and therefore no CSRF token.
	csrf := ""
	if cookie, err := r.Cookie(AdminCookieName); err == nil && cookie.Value != "" {
		if session, err := a.DB.GetSession(r.Context(), hashToken(cookie.Value)); err == nil {
			// The stored value is a digest; the console needs a token it can
			// echo back, so it re-derives one from the session hash.
			csrf = session.CSRFHash
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status":     "authenticated",
		"csrf_token": csrf,
		"admin_ui":   a.Settings.AdminUIEnabled,
		"version":    Version,
	})
}

func (a *API) handleLogout(w http.ResponseWriter, r *http.Request) {
	if err := a.Admin.Logout(r.Context(), r); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	a.Admin.ClearSessionCookie(w, r)
	writeJSON(w, http.StatusOK, map[string]any{"status": "logged_out"})
}

// ---- settings ----

// settingSchema describes one runtime-overridable setting: its default, the
// apply mode and a validator. Mirrors admin/settings_routes.SETTING_SCHEMA and
// control/settings.validate_setting.
type settingSchema struct {
	Key       string
	Default   any
	ApplyMode string
	Validate  func(any) error
}

var settingSchemas = []settingSchema{
	{"checkin.enabled", false, "scheduler_reschedule", validateBool},
	{"checkin.at", "00:10", "scheduler_reschedule", validateClock},
	{"checkin.timezone", "Asia/Shanghai", "scheduler_reschedule", validateNonEmptyString},
	{"checkin.catch_up", true, "scheduler_reschedule", validateBool},
	{"checkin.catch_up_window_hours", 6, "scheduler_reschedule", rangeValidator(0, 72, "checkin catch-up window must be between 0 and 72 hours")},
	{"checkin.jitter_min_seconds", 3, "scheduler_reschedule", rangeValidator(0, 300, "checkin jitter must be between 0 and 300 seconds")},
	{"checkin.jitter_max_seconds", 10, "scheduler_reschedule", rangeValidator(0, 300, "checkin jitter must be between 0 and 300 seconds")},
	{"checkin.retry_limit", 2, "immediate", rangeValidator(0, 10, "checkin retry limit must be between 0 and 10")},
	{"monitoring.metrics_enabled", true, "immediate", validateBool},
	{"monitoring.metrics_interval_seconds", 900, "immediate", rangeValidator(30, 86400, "metrics interval must be between 30 and 86400 seconds")},
	{"usage.rollup_interval_seconds", 60, "immediate", rangeValidator(30, 86400, "rollup interval must be between 30 and 86400 seconds")},
	{"usage.detail_retention_days", 90, "immediate", rangeValidator(1, 3650, "detail retention must be between 1 and 3650 days")},
	{"growth.auto_tasks", true, "immediate", validateBool},
	{"growth.auto_lottery", true, "immediate", validateBool},
	{"growth.auto_travel", true, "immediate", validateBool},
	{"growth.auto_redeem", true, "immediate", validateBool},
	{"growth.redeem_tier", "14d", "immediate", validateRedeemTier},
	{"growth.auto_buddy_open", false, "immediate", validateBool},
	{"growth.scheduler_enabled", true, "immediate", validateBool},
	{"growth.scheduler_interval_seconds", 1800, "immediate", rangeValidator(600, 86400, "growth.scheduler_interval_seconds must be >= 600")},
	{"growth.auto_active_day", true, "immediate", validateBool},
}

func (a *API) handleGetSettings(w http.ResponseWriter, r *http.Request) {
	stored, err := a.DB.ListRuntimeSettings(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	byKey := map[string]store.RuntimeSetting{}
	for _, setting := range stored {
		byKey[setting.Key] = setting
	}
	settings := make([]map[string]any, 0, len(settingSchemas))
	for _, schema := range settingSchemas {
		entry := map[string]any{
			"key":           schema.Key,
			"value":         schema.Default,
			"value_version": 0,
			"source":        "default",
			"apply_mode":    schema.ApplyMode,
			"apply_status":  "applied",
		}
		if setting, ok := byKey[schema.Key]; ok {
			entry["value"] = setting.Value
			entry["value_version"] = setting.Version
			entry["source"] = setting.Source
			entry["apply_status"] = setting.ApplyStatus
			entry["updated_at"] = setting.UpdatedAt
			entry["updated_by"] = setting.UpdatedBy
		}
		settings = append(settings, entry)
	}
	writeJSON(w, http.StatusOK, map[string]any{"settings": settings})
}

func (a *API) handlePatchSettings(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Settings []struct {
			Key          string `json:"key"`
			Value        any    `json:"value"`
			ValueVersion *int   `json:"value_version"`
		} `json:"settings"`
	}
	if err := decodeBody(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	schemaByKey := map[string]settingSchema{}
	for _, schema := range settingSchemas {
		schemaByKey[schema.Key] = schema
	}
	updated := make([]map[string]any, 0, len(body.Settings))
	for _, submitted := range body.Settings {
		schema, ok := schemaByKey[submitted.Key]
		if !ok {
			writeError(w, http.StatusBadRequest, "unknown setting: "+submitted.Key)
			return
		}
		if err := schema.Validate(submitted.Value); err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		status := "effective"
		if schema.ApplyMode == "control_restart_required" {
			status = "pending_restart"
		}
		version, err := a.DB.PutRuntimeSetting(r.Context(), submitted.Key, submitted.Value, schema.ApplyMode, status, "admin")
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		updated = append(updated, map[string]any{
			"key": submitted.Key, "value": submitted.Value,
			"value_version": version, "apply_mode": schema.ApplyMode, "apply_status": status,
		})
	}
	a.audit(r, "settings.update", "settings", "", map[string]any{"keys": len(updated)})
	writeJSON(w, http.StatusOK, map[string]any{"status": "applied", "settings": updated})
}

// ---- audit ----

func (a *API) handleListAudit(w http.ResponseWriter, r *http.Request) {
	limit, offset, err := pageFromRequest(r, 25)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	query := r.URL.Query()
	filter := store.AuditFilter{
		Search:       query.Get("search"),
		ActionPrefix: query.Get("action_prefix"),
		Category:     query.Get("category"),
		ResourceType: query.Get("resource_type"),
		Result:       query.Get("result"),
	}
	events, err := a.DB.ListAuditEvents(r.Context(), filter, limit, offset)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	total, err := a.DB.CountAuditEvents(r.Context(), filter)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"events":      events,
		"next_cursor": nextCursor(offset, limit, len(events)),
		"total":       total,
	})
}

// audit records one admin mutation.
func (a *API) audit(r *http.Request, action, resourceType, resourceID string, metadata map[string]any) {
	event := store.AuditEvent{
		EventID:      randomToken(16),
		ActorType:    "admin",
		Action:       action,
		ResourceType: resourceType,
		Result:       "succeeded",
		Metadata:     metadata,
	}
	if resourceID != "" {
		event.ResourceID = &resourceID
	}
	if token := bearerToken(r); token != "" && constantTimeEqual(token, a.Settings.AdminKey) {
		actor := "admin-key"
		event.ActorID = &actor
	} else {
		actor := clientIP(r)
		event.ActorID = &actor
	}
	if err := a.DB.RecordAudit(r.Context(), event); err != nil {
		// Audit is best-effort: a failure must not fail the mutation the
		// operator already performed.
		return
	}
}

// ---- backups ----

func (a *API) handleListBackups(w http.ResponseWriter, r *http.Request) {
	limit, offset, err := pageFromRequest(r, 25)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	runs, err := a.DB.ListBackupRuns(r.Context(), r.URL.Query().Get("status"), limit, offset)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"backups":     runs,
		"next_cursor": nextCursor(offset, limit, len(runs)),
	})
}

func (a *API) handleCreateBackup(w http.ResponseWriter, r *http.Request) {
	backupID := "bk-" + randomToken(8)
	directory := a.Settings.DataDir + "/backups"
	target := directory + "/" + backupID + ".sqlite3"
	if err := ensureDir(directory); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	started := store.NowISO()
	run := store.BackupRun{
		BackupID:      backupID,
		Path:          target,
		SchemaVersion: schemaVersionForBackup,
		StartedAt:     started,
		Status:        "running",
	}
	_ = a.DB.RecordBackupRun(r.Context(), run)
	ctx, cancel := ctxWithTimeout(r.Context(), 5*time.Minute)
	defer cancel()
	if err := a.DB.BackupTo(ctx, target); err != nil {
		message := err.Error()
		run.Status = "failed"
		run.ErrorMessage = &message
		finished := store.NowISO()
		run.FinishedAt = &finished
		_ = a.DB.RecordBackupRun(r.Context(), run)
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	size, digest, err := fileSizeAndDigest(target)
	if err == nil {
		run.SizeBytes = &size
		run.SHA256 = &digest
	}
	run.Status = "succeeded"
	finished := store.NowISO()
	run.FinishedAt = &finished
	_ = a.DB.RecordBackupRun(r.Context(), run)
	a.audit(r, "backup.create", "backup", backupID, nil)
	writeJSON(w, http.StatusOK, map[string]any{"status": "succeeded", "backup": run})
}

// schemaVersionForBackup is the schema generation this build writes.
const schemaVersionForBackup = "8"
