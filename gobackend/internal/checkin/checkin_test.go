package checkin

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// newTestClient points a client at a stub upstream.
func newTestClient(t *testing.T, handler http.HandlerFunc) *Client {
	t.Helper()
	upstream := httptest.NewServer(handler)
	t.Cleanup(upstream.Close)
	return NewClient(ClientOptions{
		BaseURL:      upstream.URL,
		StatusPath:   "/billing/meter/checkin-status",
		ClaimPath:    "/billing/meter/daily-checkin",
		ClaimMethod:  http.MethodPost,
		StatusMethod: "", // the default configuration disables the preflight
		Timeout:      5 * time.Second,
	})
}

func jsonBody(t *testing.T, value any) []byte {
	t.Helper()
	raw, err := json.Marshal(value)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return raw
}

// Business code 10001 on a 400 is the upstream's "already claimed today" answer,
// and it must outrank the HTTP status — otherwise a repeat run reports a failure
// for a day that is actually done.
func TestClaimClassifiesAlreadyCheckedIn(t *testing.T) {
	client := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write(jsonBody(t, map[string]any{"code": 10001, "msg": "今日已签到"}))
	})
	result, err := client.Run(context.Background(), "cb-1", Credential{Mode: "bearer", AccessToken: "t"})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if result.Outcome != OutcomeAlreadyCheckedIn {
		t.Fatalf("expected ALREADY_CHECKED_IN, got %q (%s)", result.Outcome, result.Message)
	}
	if !result.OK() {
		t.Fatal("already-checked-in must count as a success")
	}
}

func TestClaimClassifiesSuccess(t *testing.T) {
	client := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("claim must be a POST, got %s", r.Method)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer t" {
			t.Errorf("bearer header not set: %q", got)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(jsonBody(t, map[string]any{"code": 0, "msg": "ok", "rewardCredits": 15}))
	})
	result, err := client.Run(context.Background(), "cb-1", Credential{Mode: "bearer", AccessToken: "t"})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if result.Outcome != OutcomeClaimed {
		t.Fatalf("expected CLAIMED, got %q", result.Outcome)
	}
	if result.RewardCredits == nil || *result.RewardCredits != 15 {
		t.Fatalf("reward credits not extracted: %#v", result.RewardCredits)
	}
}

// The cookie must be sent on its own and the bearer must not leak when the mode
// is cookie-only: one account's session must never travel with another's token.
func TestCookieModeSendsOnlyTheCookie(t *testing.T) {
	var sawAuth, sawCookie string
	client := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		sawAuth = r.Header.Get("Authorization")
		sawCookie = r.Header.Get("Cookie")
		_, _ = w.Write(jsonBody(t, map[string]any{"code": 0}))
	})
	if _, err := client.Run(context.Background(), "cb-1",
		Credential{Mode: "cookie", Cookie: "session=abc", AccessToken: "should-not-travel"}); err != nil {
		t.Fatalf("Run: %v", err)
	}
	if sawAuth != "" {
		t.Fatalf("cookie mode must not send Authorization, got %q", sawAuth)
	}
	if sawCookie != "session=abc" {
		t.Fatalf("cookie not forwarded: %q", sawCookie)
	}
}

func TestUnsupportedAuthModeIsAuthFailure(t *testing.T) {
	client := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		t.Error("an unsupported auth mode must not reach the network")
	})
	result, _ := client.Run(context.Background(), "cb-1", Credential{Mode: "pat", AccessToken: "t"})
	if result.Outcome != OutcomeAuthFailed {
		t.Fatalf("expected AUTH_FAILED, got %q", result.Outcome)
	}
}

// HTTP status mapping must match the documented taxonomy.
func TestHTTPFailureClassification(t *testing.T) {
	cases := []struct {
		status  int
		outcome string
	}{
		{http.StatusUnauthorized, OutcomeNeedsReauth},
		{http.StatusForbidden, OutcomeNeedsReauth},
		{http.StatusTooManyRequests, OutcomeRateLimited},
		{http.StatusInternalServerError, OutcomeTransientError},
		{http.StatusBadGateway, OutcomeTransientError},
		{http.StatusBadRequest, OutcomeFailed},
	}
	for _, testCase := range cases {
		client := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(testCase.status)
			_, _ = w.Write(jsonBody(t, map[string]any{"msg": "nope"}))
		})
		result, _ := client.Run(context.Background(), "cb-1", Credential{Mode: "bearer", AccessToken: "t"})
		if result.Outcome != testCase.outcome {
			t.Errorf("status %d: expected %s, got %s", testCase.status, testCase.outcome, result.Outcome)
		}
	}
}

// The status preflight short-circuits the claim when it can already tell the day
// is claimed, which saves the write call entirely.
func TestStatusPreflightShortCircuitsClaim(t *testing.T) {
	claims := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/billing/meter/daily-checkin" {
			claims++
			_, _ = w.Write(jsonBody(t, map[string]any{"code": 0}))
			return
		}
		_, _ = w.Write(jsonBody(t, map[string]any{"code": 0, "status": "CHECKED_IN"}))
	}))
	defer upstream.Close()
	client := NewClient(ClientOptions{
		BaseURL:      upstream.URL,
		StatusPath:   "/billing/meter/checkin-status",
		ClaimPath:    "/billing/meter/daily-checkin",
		StatusMethod: http.MethodGet,
		ClaimMethod:  http.MethodPost,
		Timeout:      5 * time.Second,
	})
	result, err := client.Run(context.Background(), "cb-1", Credential{Mode: "bearer", AccessToken: "t"})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if result.Outcome != OutcomeAlreadyCheckedIn {
		t.Fatalf("expected ALREADY_CHECKED_IN from the preflight, got %q", result.Outcome)
	}
	if claims != 0 {
		t.Fatalf("the claim must not run when the preflight already answered, ran %d times", claims)
	}
}

// A failing preflight must abstain rather than fail the run: the claim is the
// authoritative call.
func TestStatusPreflightAbstainsOnTransportError(t *testing.T) {
	claims := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/billing/meter/daily-checkin" {
			claims++
			_, _ = w.Write(jsonBody(t, map[string]any{"code": 0}))
			return
		}
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer upstream.Close()
	client := NewClient(ClientOptions{
		BaseURL:      upstream.URL,
		StatusPath:   "/billing/meter/checkin-status",
		ClaimPath:    "/billing/meter/daily-checkin",
		StatusMethod: http.MethodGet,
		ClaimMethod:  http.MethodPost,
		Timeout:      5 * time.Second,
	})
	result, _ := client.Run(context.Background(), "cb-1", Credential{Mode: "bearer", AccessToken: "t"})
	if result.Outcome != OutcomeClaimed {
		t.Fatalf("a failed preflight must fall through to the claim, got %q", result.Outcome)
	}
	if claims != 1 {
		t.Fatalf("expected exactly one claim, got %d", claims)
	}
}

// The retry policy must only re-attempt outcomes the upstream can recover from.
func TestRetryPolicy(t *testing.T) {
	status := func(value int) *int { return &value }
	cases := []struct {
		name    string
		result  Result
		retries bool
	}{
		{"rate limited without status", Result{Outcome: OutcomeRateLimited}, true},
		{"rate limited 429", Result{Outcome: OutcomeRateLimited, HTTPStatus: status(429)}, true},
		{"rate limited with a 400", Result{Outcome: OutcomeRateLimited, HTTPStatus: status(400)}, false},
		{"transient without status", Result{Outcome: OutcomeTransientError}, true},
		{"transient 503", Result{Outcome: OutcomeTransientError, HTTPStatus: status(503)}, true},
		{"transient 400", Result{Outcome: OutcomeTransientError, HTTPStatus: status(400)}, false},
		{"needs reauth", Result{Outcome: OutcomeNeedsReauth, HTTPStatus: status(401)}, false},
		{"claimed", Result{Outcome: OutcomeClaimed}, false},
		{"failed", Result{Outcome: OutcomeFailed, HTTPStatus: status(400)}, false},
	}
	for _, testCase := range cases {
		if got := shouldRetry(testCase.result); got != testCase.retries {
			t.Errorf("%s: shouldRetry=%v, want %v", testCase.name, got, testCase.retries)
		}
	}
}

// retryJitter must stay within the documented exponential ceiling.
func TestRetryJitterCeiling(t *testing.T) {
	for attempt, ceiling := range map[int]int{1: 1000, 2: 2000, 5: 8000, 9: 8000} {
		for iteration := 0; iteration < 200; iteration++ {
			delay := retryJitter(attempt)
			if delay < 0 || int(delay.Milliseconds()) > ceiling {
				t.Fatalf("attempt %d produced %v ms, ceiling %d", attempt, delay.Milliseconds(), ceiling)
			}
		}
	}
}

// parseClock must survive a malformed schedule rather than panicking.
func TestParseClock(t *testing.T) {
	cases := map[string][2]int{
		"00:10":    {0, 10},
		"10:30":    {10, 30},
		"23:59":    {23, 59},
		"":         {0, 10},
		"nonsense": {0, 10},
		"25:00":    {0, 10},
		"12:99":    {0, 10},
	}
	for input, expected := range cases {
		hour, minute := parseClock(input)
		if hour != expected[0] || minute != expected[1] {
			t.Errorf("parseClock(%q) = (%d,%d), want (%d,%d)", input, hour, minute, expected[0], expected[1])
		}
	}
}
