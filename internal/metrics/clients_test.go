package metrics

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/dmego/qoderbuddy2api/internal/config"
	"github.com/dmego/qoderbuddy2api/internal/models"
)

// creditsBody is the upstream envelope normalizeCredits expects.
func creditsBody() string {
	return `{"code":0,"data":{"Response":{"Data":{"Accounts":[
	  {"CapacityUnit":"credits","CapacityRemain":120,"CapacityUsed":30,"CapacitySize":150,
	   "CycleCapacityRemain":120,"CycleCapacitySize":150,"ExpiredTime":"",
	   "CycleEndTime":"2026-10-01T00:00:00+00:00","PackageName":"基础积分包"},
	  {"CapacityUnit":"credits","CapacityRemain":0,"CapacityUsed":10,"CapacitySize":10,
	   "ExpiredTime":1759276800000,"ProductName":"活动积分包"}
	]}}}}`
}

// TestCreditClientSendsBrowserFingerprint pins the request shape the billing
// endpoints require: without the browser headers and the empty JSON body the
// upstream answers 403, which the metric can only report as a failure.
func TestCreditClientSendsBrowserFingerprint(t *testing.T) {
	var (
		method  string
		body    string
		headers http.Header
	)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		method = r.Method
		raw, _ := io.ReadAll(r.Body)
		body = string(raw)
		headers = r.Header.Clone()
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, creditsBody())
	}))
	defer server.Close()

	client := NewCreditClients(config.Settings{
		CodeBuddyCheckinBase:     server.URL,
		CodeBuddyCreditsPath:     "/billing/meter/get-user-resource",
		WorkBuddyIntlEndpoint:    server.URL,
		WorkBuddyIntlCreditsPath: "/billing/meter/get-user-resource",
		CheckinTimeout:           5,
	})
	value, err := client.FetchCredits(context.Background(), models.ProviderWorkBuddy, "cb-test",
		Credential{Mode: "bearer", AccessToken: "secret-token"})
	if err != nil {
		t.Fatalf("fetch credits: %v", err)
	}

	if method != http.MethodPost {
		t.Fatalf("method = %q, want POST", method)
	}
	if body != "{}" {
		t.Fatalf("body = %q, want {}", body)
	}
	if got := headers.Get("Authorization"); got != "Bearer secret-token" {
		t.Fatalf("Authorization = %q", got)
	}
	for name, want := range map[string]string{
		"Origin":            server.URL,
		"Referer":           server.URL + "/",
		"X-Client-Platform": "web",
	} {
		if got := headers.Get(name); got != want {
			t.Fatalf("%s = %q, want %q", name, got, want)
		}
	}
	if agent := headers.Get("User-Agent"); agent != browserUserAgent {
		t.Fatalf("User-Agent = %q, want the Chrome desktop identity", agent)
	}

	// The aggregate is what the credits page renders.
	if value["total_remaining"] != 120 || value["total_used"] != 40 || value["total_capacity"] != 160 {
		t.Fatalf("aggregate totals = %#v", value)
	}
	if value["package_count"] != 2 || value["depleted_packages"] != 1 {
		t.Fatalf("package counts = %#v", value)
	}
	if value["lowest_remaining"] != 0 {
		t.Fatalf("lowest_remaining = %#v, want 0", value["lowest_remaining"])
	}
	packages := packageRows(t, value)
	if len(packages) != 2 {
		t.Fatalf("packages = %#v", value["packages"])
	}
	// An empty ExpiredTime falls back to CycleEndTime, which is the expiry the
	// package's own usage page shows.
	if packages[0]["expires_at"] != "2026-10-01T00:00:00+00:00" {
		t.Fatalf("first package expiry = %#v", packages[0]["expires_at"])
	}
	if packages[0]["name"] != "基础积分包" {
		t.Fatalf("first package name = %#v", packages[0]["name"])
	}
	// A blank name falls back to the positional label rather than an empty cell.
	if packages[1]["name"] != "活动积分包" {
		t.Fatalf("second package name = %#v", packages[1]["name"])
	}
	// expires_at prefers the earliest expiry across packages.
	if value["expires_at"] == nil {
		t.Fatal("aggregate expiry is nil")
	}
}

// TestCreditClientRejectsNonZeroCode covers the envelope check: a response that
// is not a success must not be stored as a balance.
func TestCreditClientRejectsNonZeroCode(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"code":10085,"msg":"blocked"}`)
	}))
	defer server.Close()
	client := NewCreditClients(config.Settings{
		CodeBuddyCheckinBase: server.URL,
		CodeBuddyCreditsPath: "/billing/meter/get-user-resource",
		CheckinTimeout:       5,
	})
	if _, err := client.FetchCredits(context.Background(), models.ProviderWorkBuddy, "cb-test",
		Credential{AccessToken: "secret-token"}); err == nil {
		t.Fatal("non-zero business code was accepted as a balance")
	}
}

// TestCheckinStatusHonoursStatusMethod covers the domestic preflight switch: an
// unset method skips the call entirely, which is how the default deployment runs.
func TestCheckinStatusHonoursStatusMethod(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls++
		_, _ = io.WriteString(w, `{"code":0,"status":"CHECKED_IN","checkedIn":true}`)
	}))
	defer server.Close()

	disabled := NewCreditClients(config.Settings{
		CodeBuddyCheckinBase: server.URL, CheckinTimeout: 5,
	})
	value, err := disabled.FetchCheckinStatus(context.Background(), models.ProviderWorkBuddy, "cb-test",
		Credential{AccessToken: "secret-token"})
	if err != nil {
		t.Fatalf("fetch checkin status: %v", err)
	}
	if value != nil || calls != 0 {
		t.Fatalf("status preflight ran with an empty method: value=%#v calls=%d", value, calls)
	}

	enabled := NewCreditClients(config.Settings{
		CodeBuddyCheckinBase:         server.URL,
		CodeBuddyCheckinStatusPath:   "/billing/meter/checkin-status",
		CodeBuddyCheckinStatusMethod: "GET",
		CheckinTimeout:               5,
	})
	value, err = enabled.FetchCheckinStatus(context.Background(), models.ProviderWorkBuddy, "cb-test",
		Credential{AccessToken: "secret-token"})
	if err != nil {
		t.Fatalf("fetch checkin status: %v", err)
	}
	if calls != 1 {
		t.Fatalf("status preflight calls = %d, want 1", calls)
	}
	if value["status"] != "CHECKED_IN" || value["checkedIn"] != true {
		t.Fatalf("status summary = %#v", value)
	}
	if value["business_code"] != "0" {
		t.Fatalf("business code = %#v, want the upstream code as a string", value["business_code"])
	}
}

// TestIntlProviderHasNoCheckinRead pins that the international deployment never
// issues a check-in call, which has no endpoint there.
func TestIntlProviderHasNoCheckinRead(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		t.Error("international provider issued a request")
	}))
	defer server.Close()
	client := NewCreditClients(config.Settings{
		WorkBuddyIntlEndpoint:        server.URL,
		CodeBuddyCheckinStatusMethod: "GET",
		CheckinTimeout:               5,
	})
	value, err := client.FetchCheckinStatus(context.Background(), models.ProviderWorkBuddyIntl, "intl-test",
		Credential{AccessToken: "secret-token"})
	if err != nil {
		t.Fatalf("fetch checkin status: %v", err)
	}
	if value != nil {
		t.Fatalf("international check-in summary = %#v, want nil", value)
	}
}

// TestJoinURLAndOrigin covers the base/path composition the two providers use,
// including a base URL that already ends in a slash.
func TestJoinURLAndOrigin(t *testing.T) {
	if got := joinURL("https://www.workbuddy.cn/", "/billing/meter/get-user-resource"); got != "https://www.workbuddy.cn/billing/meter/get-user-resource" {
		t.Fatalf("joinURL = %q", got)
	}
	if got := originOf("https://www.workbuddy.ai/base/path"); got != "https://www.workbuddy.ai" {
		t.Fatalf("originOf = %q", got)
	}
}

// packageRows reads the rendered package list from a credit aggregate.
func packageRows(t *testing.T, value map[string]any) []map[string]any {
	t.Helper()
	raw, ok := value["packages"].([]any)
	if !ok {
		t.Fatalf("packages is %T, want an array", value["packages"])
	}
	out := make([]map[string]any, 0, len(raw))
	for _, entry := range raw {
		row, ok := entry.(map[string]any)
		if !ok {
			t.Fatalf("package entry is %T, want an object", entry)
		}
		out = append(out, row)
	}
	return out
}

// TestNormalizeCreditsIgnoresMalformedRows pins that a partial upstream payload
// still yields the rows it can rather than dropping the whole balance.
func TestNormalizeCreditsIgnoresMalformedRows(t *testing.T) {
	var body map[string]any
	if err := json.Unmarshal([]byte(`{"code":"0","data":{"Response":{"Data":{"Accounts":[
	  "not-an-object",{"CapacityRemain":"120","CapacityUsed":true,"PackageName":"   "}
	]}}}}`), &body); err != nil {
		t.Fatalf("decode fixture: %v", err)
	}
	value := normalizeCredits(body)
	if value["package_count"] != 2 {
		t.Fatalf("package_count = %#v", value["package_count"])
	}
	if value["total_remaining"] != 120 {
		t.Fatalf("total_remaining = %#v, want the parsable row only", value["total_remaining"])
	}
	// A boolean usage value coerces to 0 instead of poisoning the total.
	if value["total_used"] != 0 {
		t.Fatalf("total_used = %#v, want 0", value["total_used"])
	}
	packages := packageRows(t, value)
	if len(packages) != 1 {
		t.Fatalf("packages = %#v, want only the parsable row", packages)
	}
	// The positional fallback counts rendered packages, so the skipped
	// non-object row does not consume an index (matching the Python original).
	if packages[0]["name"] != "积分包 1" {
		t.Fatalf("blank package name = %#v, want the positional fallback", packages[0]["name"])
	}
}
