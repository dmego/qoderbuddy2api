package server

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/checkin"
	"github.com/dmego/qoderbuddy2api/internal/config"
	"github.com/dmego/qoderbuddy2api/internal/store"
	"github.com/dmego/qoderbuddy2api/internal/vault"
)

const testAdminKey = "test-admin-key"

// newTestAPI builds an API over a real SQLite database and a real vault.
func newTestAPI(t *testing.T) (*API, *store.DB) {
	t.Helper()
	db, err := store.Open(filepath.Join(t.TempDir(), "qb2api.sqlite3"))
	if err != nil {
		t.Fatalf("open db: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	credVault, err := vault.New("fMiLyPKzgrsAqJ24XxFvLZeuLuSCfV2gMV3-Lb3Z-7U=")
	if err != nil {
		t.Fatalf("vault: %v", err)
	}
	settings := config.Settings{
		DataDir:         t.TempDir(),
		LogDir:          t.TempDir(),
		AdminKey:        testAdminKey,
		CheckinAt:       "00:10",
		CheckinTimezone: "Asia/Shanghai",
	}
	events := store.NewEventWriter(db, time.Second, 10)
	t.Cleanup(func() { _ = events.Close(context.Background()) })
	// A nil plane is tolerated by every handler these tests exercise.
	api := NewAPI(settings, db, credVault, events, nil, t.TempDir())
	return api, db
}

// adminRequest performs an authenticated request against the API.
func adminRequest(t *testing.T, api *API, method, target string, body any) *httptest.ResponseRecorder {
	t.Helper()
	request := httptest.NewRequest(method, target, jsonBodyReader(t, body))
	request.Header.Set("Authorization", "Bearer "+testAdminKey)
	request.Header.Set("Content-Type", "application/json")
	recorder := httptest.NewRecorder()
	api.Handler().ServeHTTP(recorder, request)
	return recorder
}

// jsonBodyReader renders a request body, or an empty reader for a nil body.
func jsonBodyReader(t *testing.T, body any) io.Reader {
	t.Helper()
	if body == nil {
		return nil
	}
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatalf("marshal body: %v", err)
	}
	return bytes.NewReader(raw)
}

// The console decides which control to render from the schema block and its
// "type" strings. Without the block it falls back to JavaScript's typeof, which
// yields "boolean" instead of "bool" and renders no control at all — the page
// looks broken while the API still answers 200.
func TestSettingsResponseCarriesSchema(t *testing.T) {
	api, _ := newTestAPI(t)
	recorder := adminRequest(t, api, http.MethodGet, "/api/admin/settings", nil)
	if recorder.Code != http.StatusOK {
		t.Fatalf("status %d: %s", recorder.Code, recorder.Body.String())
	}
	var response struct {
		Settings []map[string]any          `json:"settings"`
		Schema   map[string]map[string]any `json:"schema"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(response.Settings) == 0 {
		t.Fatal("no settings returned")
	}
	if len(response.Schema) == 0 {
		t.Fatal("schema block missing: the console cannot render any control without it")
	}
	for _, key := range []string{"checkin.enabled", "checkin.at", "growth.redeem_tier", "growth.scheduler_interval_seconds"} {
		entry, ok := response.Schema[key]
		if !ok {
			t.Fatalf("schema entry missing for %s", key)
		}
		kind, _ := entry["type"].(string)
		switch kind {
		case "bool", "int", "str":
		default:
			t.Fatalf("%s has type %q, want bool/int/str", key, kind)
		}
		if _, ok := entry["default"]; !ok {
			t.Fatalf("%s schema entry has no default", key)
		}
	}
	// Every setting must have a schema entry, or its row renders uncontrolled.
	for _, setting := range response.Settings {
		key, _ := setting["key"].(string)
		if _, ok := response.Schema[key]; !ok {
			t.Fatalf("setting %s has no schema entry", key)
		}
	}
}

// Settings are saved one at a time as a bare object; a wrapper-shaped body
// would be rejected as an unknown setting and nothing would ever save.
func TestPatchSettingAcceptsBareObject(t *testing.T) {
	api, db := newTestAPI(t)
	recorder := adminRequest(t, api, http.MethodPatch, "/api/admin/settings",
		map[string]any{"key": "checkin.at", "value": "07:45", "value_version": 0})
	if recorder.Code != http.StatusOK {
		t.Fatalf("status %d: %s", recorder.Code, recorder.Body.String())
	}
	var response map[string]any
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if response["value"] != "07:45" {
		t.Fatalf("value not applied: %#v", response)
	}
	if _, ok := response["value_version"]; !ok {
		t.Fatal("response must report value_version for the console's optimistic update")
	}
	stored, err := db.RuntimeSettingsMap(t.Context())
	if err != nil {
		t.Fatalf("read back: %v", err)
	}
	if stored["checkin.at"] != "07:45" {
		t.Fatalf("setting not persisted: %#v", stored["checkin.at"])
	}
}

func TestPatchSettingRejectsBadInput(t *testing.T) {
	api, _ := newTestAPI(t)
	cases := []struct {
		name string
		body map[string]any
		want int
	}{
		{"unknown key", map[string]any{"key": "nope.nope", "value": 1}, http.StatusBadRequest},
		{"wrong type", map[string]any{"key": "checkin.enabled", "value": "yes"}, http.StatusBadRequest},
		{"out of range", map[string]any{"key": "checkin.retry_limit", "value": 99}, http.StatusBadRequest},
		{"bad clock", map[string]any{"key": "checkin.at", "value": "99:99"}, http.StatusBadRequest},
		{"bad tier", map[string]any{"key": "growth.redeem_tier", "value": "3d"}, http.StatusBadRequest},
	}
	for _, testCase := range cases {
		recorder := adminRequest(t, api, http.MethodPatch, "/api/admin/settings", testCase.body)
		if recorder.Code != testCase.want {
			t.Errorf("%s: status %d, want %d (%s)", testCase.name, recorder.Code, testCase.want, recorder.Body.String())
		}
	}
}

// A stale version must be refused rather than overwriting another
// administrator's newer value.
func TestPatchSettingRejectsStaleVersion(t *testing.T) {
	api, _ := newTestAPI(t)
	first := adminRequest(t, api, http.MethodPatch, "/api/admin/settings",
		map[string]any{"key": "checkin.at", "value": "05:00", "value_version": 0})
	if first.Code != http.StatusOK {
		t.Fatalf("first save failed: %d %s", first.Code, first.Body.String())
	}
	stale := adminRequest(t, api, http.MethodPatch, "/api/admin/settings",
		map[string]any{"key": "checkin.at", "value": "06:00", "value_version": 0})
	if stale.Code != http.StatusConflict {
		t.Fatalf("stale write must conflict, got %d %s", stale.Code, stale.Body.String())
	}
}

// The console compares the outcome against lowercase literals. Returning the
// stored uppercase value silently renders every successful check-in as a
// failure, and blanks the reward column.
func TestCheckinAttemptOutcomeIsLowerCase(t *testing.T) {
	api, db := newTestAPI(t)
	ctx := t.Context()

	if err := db.CreateCheckinRun(ctx, store.CheckinRun{
		RunID: "run-1", LocalDate: "2026-09-16", Timezone: "Asia/Shanghai",
		StartedAt: store.NowISO(), Status: "finished", Trigger: "manual",
	}); err != nil {
		t.Fatalf("seed run: %v", err)
	}
	claimed := checkin.OutcomeClaimed
	skipped := checkin.OutcomeSkipped
	code := "0"
	reward := 15.0
	if err := db.UpsertCheckinAttempt(ctx, store.CheckinAttempt{
		RunID: "run-1", Provider: "codebuddy", AccountID: "cb-1",
		Outcome: &claimed, BusinessCode: &code, RewardCredits: &reward, Attempts: 1,
	}); err != nil {
		t.Fatalf("seed attempt: %v", err)
	}
	if err := db.UpsertCheckinAttempt(ctx, store.CheckinAttempt{
		RunID: "run-1", Provider: "codebuddy", AccountID: "cb-2",
		Outcome: &skipped, Attempts: 0,
	}); err != nil {
		t.Fatalf("seed attempt 2: %v", err)
	}
	if err := db.UpsertCheckinDailyState(ctx, store.CheckinDailyState{
		Provider: "codebuddy", AccountID: "cb-1", LocalDate: "2026-09-16",
		Timezone: "Asia/Shanghai", TerminalOutcome: &claimed,
	}); err != nil {
		t.Fatalf("seed daily state: %v", err)
	}

	recorder := adminRequest(t, api, http.MethodGet, "/api/admin/checkin/runs/run-1", nil)
	if recorder.Code != http.StatusOK {
		t.Fatalf("status %d: %s", recorder.Code, recorder.Body.String())
	}
	var detail struct {
		Run struct {
			AttemptCount    int `json:"attempt_count"`
			SuccessfulCount int `json:"successful_count"`
		} `json:"run"`
		Attempts []map[string]any `json:"attempts"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &detail); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(detail.Attempts) != 2 {
		t.Fatalf("expected 2 attempts, got %d", len(detail.Attempts))
	}
	byAccount := map[string]map[string]any{}
	for _, attempt := range detail.Attempts {
		byAccount[attempt["account_id"].(string)] = attempt
	}
	if got := byAccount["cb-1"]["outcome"]; got != "claimed" {
		t.Fatalf("outcome must be lowercase for the console, got %#v", got)
	}
	if got := byAccount["cb-1"]["reward_credits"]; got != float64(15) {
		t.Fatalf("reward must be visible on a claim, got %#v", got)
	}
	// A skipped attempt must not report a reward, or the console shows a credit
	// that never happened.
	if got := byAccount["cb-2"]["reward_credits"]; got != nil {
		t.Fatalf("skipped attempt must not report a reward, got %#v", got)
	}
	// SKIPPED counts as a successful batch outcome.
	if detail.Run.SuccessfulCount != 2 {
		t.Fatalf("expected 2 successful (claimed + skipped), got %d", detail.Run.SuccessfulCount)
	}
	if detail.Run.AttemptCount != 2 {
		t.Fatalf("expected attempt_count 2, got %d", detail.Run.AttemptCount)
	}
}

// The console labels the scheduler trigger "scheduled" while the database stores
// "scheduler"; passing the label through unchanged matches nothing.
func TestCheckinRunsScheduledTriggerFilter(t *testing.T) {
	api, db := newTestAPI(t)
	ctx := t.Context()
	for _, run := range []store.CheckinRun{
		{RunID: "run-sched", LocalDate: "2026-09-16", Timezone: "Asia/Shanghai", StartedAt: store.NowISO(), Status: "finished", Trigger: "scheduler"},
		{RunID: "run-manual", LocalDate: "2026-09-16", Timezone: "Asia/Shanghai", StartedAt: store.NowISO(), Status: "finished", Trigger: "manual"},
	} {
		if err := db.CreateCheckinRun(ctx, run); err != nil {
			t.Fatalf("seed run: %v", err)
		}
	}

	recorder := adminRequest(t, api, http.MethodGet, "/api/admin/checkin/runs?trigger=scheduled", nil)
	if recorder.Code != http.StatusOK {
		t.Fatalf("status %d: %s", recorder.Code, recorder.Body.String())
	}
	var response struct {
		Runs  []map[string]any `json:"runs"`
		Total int              `json:"total"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(response.Runs) != 1 {
		t.Fatalf("scheduled filter must match exactly the scheduler run, got %d: %#v", len(response.Runs), response.Runs)
	}
	if response.Runs[0]["run_id"] != "run-sched" {
		t.Fatalf("wrong run matched: %#v", response.Runs[0])
	}
	// The run view must carry the aggregate counters the table renders.
	for _, key := range []string{"attempt_count", "successful_count", "finished_at", "status", "trigger"} {
		if _, ok := response.Runs[0][key]; !ok {
			t.Fatalf("run view missing %q: %#v", key, response.Runs[0])
		}
	}
}

// The status page dereferences optional blocks; a nil where an object belongs
// blanks the page.
func TestCheckinStatusShapeIsComplete(t *testing.T) {
	api, _ := newTestAPI(t)
	recorder := adminRequest(t, api, http.MethodGet, "/api/admin/checkin/status", nil)
	if recorder.Code != http.StatusOK {
		t.Fatalf("status %d: %s", recorder.Code, recorder.Body.String())
	}
	var response map[string]any
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatalf("decode: %v", err)
	}
	for _, key := range []string{"enabled", "running", "local_date", "timezone", "checkin_at", "eligible_accounts", "daily_states"} {
		if _, ok := response[key]; !ok {
			t.Fatalf("status missing %q: %#v", key, response)
		}
	}
	for _, key := range []string{"scheduler", "metrics"} {
		if _, ok := response[key].(map[string]any); !ok {
			t.Fatalf("%s must be an object, got %#v", key, response[key])
		}
	}
}

// The console logs in, then re-fetches its CSRF token on every page load before
// any save. A token that cannot be verified makes every save fail silently with
// a 403 while the page still looks healthy — the exact failure this locks in.
func TestSessionCSRFTokenCanBeUsedToSave(t *testing.T) {
	api, _ := newTestAPI(t)
	handler := api.Handler()

	// 1. Log in with the admin key; the response carries the cookie.
	login := httptest.NewRequest(http.MethodPost, "/api/admin/session",
		jsonBodyReader(t, map[string]any{"admin_key": testAdminKey}))
	// httptest defaults RemoteAddr to a documentation address, which the cookie
	// policy treats as a remote (non-loopback) peer and refuses over plain HTTP.
	login.RemoteAddr = "127.0.0.1:54321"
	login.Header.Set("Content-Type", "application/json")
	loginRecorder := httptest.NewRecorder()
	handler.ServeHTTP(loginRecorder, login)
	if loginRecorder.Code != http.StatusOK {
		t.Fatalf("login failed: %d %s", loginRecorder.Code, loginRecorder.Body.String())
	}
	cookies := loginRecorder.Result().Cookies()
	if len(cookies) == 0 {
		t.Fatal("login did not set a session cookie")
	}
	var sessionCookie *http.Cookie
	for _, cookie := range cookies {
		if cookie.Name == AdminCookieName {
			sessionCookie = cookie
		}
	}
	if sessionCookie == nil {
		t.Fatalf("session cookie %s missing from %v", AdminCookieName, cookies)
	}

	// 2. Re-fetch the session, which is how the console learns its CSRF token.
	info := httptest.NewRequest(http.MethodGet, "/api/admin/session", nil)
	info.RemoteAddr = "127.0.0.1:54321"
	info.AddCookie(sessionCookie)
	infoRecorder := httptest.NewRecorder()
	handler.ServeHTTP(infoRecorder, info)
	if infoRecorder.Code != http.StatusOK {
		t.Fatalf("session info failed: %d %s", infoRecorder.Code, infoRecorder.Body.String())
	}
	var sessionResponse struct {
		Status    string `json:"status"`
		CSRFToken string `json:"csrf_token"`
	}
	if err := json.Unmarshal(infoRecorder.Body.Bytes(), &sessionResponse); err != nil {
		t.Fatalf("decode session: %v", err)
	}
	if sessionResponse.CSRFToken == "" {
		t.Fatal("session response returned no csrf_token: every subsequent save would 403")
	}

	// 3. Save a setting with only the cookie and the fetched token — exactly what
	//    the console sends.
	save := httptest.NewRequest(http.MethodPatch, "/api/admin/settings",
		jsonBodyReader(t, map[string]any{"key": "checkin.at", "value": "07:45", "value_version": 0}))
	save.RemoteAddr = "127.0.0.1:54321"
	save.Header.Set("Content-Type", "application/json")
	save.Header.Set(CSRFHeaderName, sessionResponse.CSRFToken)
	save.AddCookie(sessionCookie)
	saveRecorder := httptest.NewRecorder()
	handler.ServeHTTP(saveRecorder, save)
	if saveRecorder.Code != http.StatusOK {
		t.Fatalf("cookie+CSRF save failed: %d %s", saveRecorder.Code, saveRecorder.Body.String())
	}

	// 4. A save without the header must still be refused.
	unsigned := httptest.NewRequest(http.MethodPatch, "/api/admin/settings",
		jsonBodyReader(t, map[string]any{"key": "checkin.at", "value": "09:00", "value_version": 1}))
	unsigned.RemoteAddr = "127.0.0.1:54321"
	unsigned.Header.Set("Content-Type", "application/json")
	unsigned.AddCookie(sessionCookie)
	unsignedRecorder := httptest.NewRecorder()
	handler.ServeHTTP(unsignedRecorder, unsigned)
	if unsignedRecorder.Code != http.StatusForbidden {
		t.Fatalf("a cookie request without CSRF must be refused, got %d", unsignedRecorder.Code)
	}
}
