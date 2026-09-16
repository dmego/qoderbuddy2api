package server

import (
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/store"
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
	// The console re-fetches its CSRF token on every page load. The token is
	// derived from the session id, so it is reproducible from the cookie alone;
	// returning the stored digest instead would hand the console a value that
	// can never verify, and every save would fail with a silent 403.
	csrf := any(nil)
	if cookie, err := r.Cookie(AdminCookieName); err == nil && cookie.Value != "" {
		csrf = a.Admin.csrfToken(cookie.Value)
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status":          "ok",
		"authenticated":   true,
		"csrf_token":      csrf,
		"admin_ui":        a.Settings.AdminUIEnabled,
		"version":         Version,
		"active_sessions": a.Admin.ActiveSessionCount(r.Context()),
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
	// Type is the wire type name the console switches on: "bool", "int" or "str".
	Type            string
	RestartRequired bool
	// Min is surfaced in the public schema so the console can bound its input;
	// Validate below remains the authority.
	Min      *int
	Validate func(any) error
}

var settingSchemas = []settingSchema{
	// The worker lifecycle settings exist for console parity with the Python
	// build. The Go rewrite serves the proxy and the control plane from one
	// process, so there is no worker to autostart and no start handshake to time
	// out; the values are stored and reported but have no effect.
	{Key: "service.worker.autostart", Default: false, ApplyMode: "control_restart_required", Type: "bool", RestartRequired: true, Validate: validateBool},
	{Key: "service.worker.start_timeout_seconds", Default: 30, ApplyMode: "worker_restart", Type: "int", Validate: rangeValidator(5, 300, "worker start timeout must be between 5 and 300 seconds")},

	{Key: "checkin.enabled", Default: false, ApplyMode: "scheduler_reschedule", Type: "bool", Validate: validateBool},
	{Key: "checkin.at", Default: "00:10", ApplyMode: "scheduler_reschedule", Type: "str", Validate: validateClock},
	{Key: "checkin.timezone", Default: "Asia/Shanghai", ApplyMode: "scheduler_reschedule", Type: "str", Validate: validateNonEmptyString},
	{Key: "checkin.catch_up", Default: true, ApplyMode: "scheduler_reschedule", Type: "bool", Validate: validateBool},
	{Key: "checkin.catch_up_window_hours", Default: 6, ApplyMode: "scheduler_reschedule", Type: "int", Validate: rangeValidator(0, 72, "checkin catch-up window must be between 0 and 72 hours")},
	{Key: "checkin.jitter_min_seconds", Default: 3, ApplyMode: "scheduler_reschedule", Type: "int", Validate: rangeValidator(0, 300, "checkin jitter must be between 0 and 300 seconds")},
	{Key: "checkin.jitter_max_seconds", Default: 10, ApplyMode: "scheduler_reschedule", Type: "int", Validate: rangeValidator(0, 300, "checkin jitter must be between 0 and 300 seconds")},
	{Key: "checkin.retry_limit", Default: 2, ApplyMode: "immediate", Type: "int", Validate: rangeValidator(0, 10, "checkin retry limit must be between 0 and 10")},

	{Key: "monitoring.metrics_enabled", Default: true, ApplyMode: "immediate", Type: "bool", Validate: validateBool},
	{Key: "monitoring.metrics_interval_seconds", Default: 900, ApplyMode: "immediate", Type: "int", Validate: rangeValidator(30, 86400, "metrics interval must be between 30 and 86400 seconds")},

	{Key: "usage.rollup_interval_seconds", Default: 60, ApplyMode: "immediate", Type: "int", Validate: rangeValidator(10, 3600, "rollup interval must be between 10 and 3600 seconds")},
	{Key: "usage.detail_retention_days", Default: 90, ApplyMode: "immediate", Type: "int", Validate: rangeValidator(1, 3650, "detail retention must be between 1 and 3650 days")},

	{Key: "growth.auto_tasks", Default: true, ApplyMode: "immediate", Type: "bool", Validate: validateBool},
	{Key: "growth.auto_lottery", Default: true, ApplyMode: "immediate", Type: "bool", Validate: validateBool},
	{Key: "growth.auto_travel", Default: true, ApplyMode: "immediate", Type: "bool", Validate: validateBool},
	{Key: "growth.auto_redeem", Default: true, ApplyMode: "immediate", Type: "bool", Validate: validateBool},
	{Key: "growth.redeem_tier", Default: "28d", ApplyMode: "immediate", Type: "str", Validate: validateRedeemTier},
	{Key: "growth.auto_buddy_open", Default: false, ApplyMode: "immediate", Type: "bool", Validate: validateBool},
	{Key: "growth.scheduler_enabled", Default: true, ApplyMode: "immediate", Type: "bool", Validate: validateBool},
	{Key: "growth.scheduler_interval_seconds", Default: 1800, ApplyMode: "immediate", Type: "int", Min: intPointer(600), Validate: rangeValidator(600, 86400, "growth.scheduler_interval_seconds must be >= 600")},
	{Key: "growth.auto_active_day", Default: true, ApplyMode: "immediate", Type: "bool", Validate: validateBool},
}

// settingSchemaByKey indexes the schema table once.
func settingSchemaByKey() map[string]settingSchema {
	out := make(map[string]settingSchema, len(settingSchemas))
	for _, schema := range settingSchemas {
		out[schema.Key] = schema
	}
	return out
}

// publicSchema renders the schema block the console reads to decide which
// control to draw for each setting.
//
// The type name is a string ("bool"/"int"/"str") and it is load-bearing: the
// console's schemaType() falls back to JavaScript's typeof when the key is
// missing, which yields "boolean" instead of "bool" and silently renders no
// control at all.
func publicSchema() map[string]map[string]any {
	out := make(map[string]map[string]any, len(settingSchemas))
	for _, schema := range settingSchemas {
		entry := map[string]any{
			"default":    schema.Default,
			"apply_mode": schema.ApplyMode,
			"type":       schema.Type,
		}
		if schema.RestartRequired {
			entry["restart_required"] = true
		}
		if schema.Min != nil {
			entry["min"] = *schema.Min
		}
		out[schema.Key] = entry
	}
	return out
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
		// Defaults render as an effective stored value so the console has
		// something to bind its inputs to before anything is ever saved.
		entry := map[string]any{
			"key":              schema.Key,
			"value":            schema.Default,
			"value_version":    0,
			"source":           "default",
			"apply_mode":       schema.ApplyMode,
			"apply_status":     "effective",
			"restart_required": schema.RestartRequired,
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
	writeJSON(w, http.StatusOK, map[string]any{"settings": settings, "schema": publicSchema()})
}

// handlePatchSettings applies one setting.
//
// The console saves settings one at a time and sends a bare object
// ({key, value, value_version}), not a wrapper, so the body is read as a single
// setting. A version mismatch is reported as a conflict rather than silently
// overwriting a newer value written by another administrator.
func (a *API) handlePatchSettings(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Key          string `json:"key"`
		Value        any    `json:"value"`
		ValueVersion *int   `json:"value_version"`
	}
	if err := decodeBody(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	schema, ok := settingSchemaByKey()[body.Key]
	if !ok {
		writeError(w, http.StatusBadRequest, "unknown_setting")
		return
	}
	if !matchesSchemaType(schema.Type, body.Value) {
		writeError(w, http.StatusBadRequest, "invalid_setting_type")
		return
	}
	if err := schema.Validate(body.Value); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	// A stale version means the operator is editing a snapshot someone else has
	// already replaced; refusing is the only outcome that cannot lose data.
	//
	// Version 0 is the console's marker for "never saved, showing the default",
	// so it is accepted for a key with no stored row. Any other mismatch — a
	// newer stored version, or a save racing a first write — conflicts.
	if body.ValueVersion != nil {
		stored, err := a.DB.ListRuntimeSettings(r.Context())
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		current := 0
		for _, setting := range stored {
			if setting.Key == body.Key {
				current = setting.Version
			}
		}
		if current != *body.ValueVersion {
			writeError(w, http.StatusConflict, "setting_version_conflict")
			return
		}
	}
	status := "effective"
	if schema.ApplyMode == "control_restart_required" {
		status = "pending_restart"
	}
	version, err := a.DB.PutRuntimeSetting(r.Context(), body.Key, body.Value, schema.ApplyMode, status, "admin")
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	a.audit(r, "settings.update", "setting", body.Key, map[string]any{"apply_status": status})
	a.applySetting(body.Key)
	writeJSON(w, http.StatusOK, map[string]any{
		"key":              body.Key,
		"value":            body.Value,
		"value_version":    version,
		"apply_mode":       schema.ApplyMode,
		"apply_status":     status,
		"restart_required": schema.RestartRequired,
	})
}

// matchesSchemaType enforces the declared type, rejecting a string where a
// number belongs before anything is persisted.
func matchesSchemaType(kind string, value any) bool {
	switch kind {
	case "bool":
		_, ok := value.(bool)
		return ok
	case "int":
		number, ok := value.(float64)
		return ok && number == float64(int(number))
	case "str":
		_, ok := value.(string)
		return ok
	}
	return false
}

// applySetting re-reads a setting into the live schedulers.
//
// The schedulers read runtime settings on each tick, so a schedule change only
// needs the check-in scheduler to recompute its timer.
func (a *API) applySetting(key string) {
	if a.CheckinScheduler == nil {
		return
	}
	if strings.HasPrefix(key, "checkin.") {
		a.CheckinScheduler.Reconfigure()
	}
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
		"events":      list(events),
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
		"backups":     list(runs),
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
