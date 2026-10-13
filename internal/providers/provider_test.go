package providers

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"syscall"
	"testing"
	"time"
)

// TestErrorCodeClassifiesFailures pins the telemetry contract: error_code is a
// stable classification, not the Go type of the error. The recorder used to
// store fmt.Sprintf("%T", err), which reports *fmt.wrapError / *errors.errorString
// for every wrapped failure and therefore cannot be grouped by.
func TestErrorCodeClassifiesFailures(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want string
	}{
		{"nil", nil, ""},
		{"upstream status", &UpstreamError{StatusCode: 502, Message: "bad gateway"}, "upstream_error"},
		{"channel blocked", &ChannelBlockedError{UpstreamError{StatusCode: 400, Message: "x"}}, "channel_blocked"},
		{"quota exceeded", &QuotaExceededError{UpstreamError: UpstreamError{StatusCode: 400, Message: "x"}}, "quota_exceeded"},
		{"no routes", fmt.Errorf("deepseek-v4.1-flash: %w", UnavailableError), "no_available_routes"},
		{"client cancel", fmt.Errorf("post: %w", context.Canceled), "client_canceled"},
		{"deadline", context.DeadlineExceeded, "deadline_exceeded"},
		{"truncated", io.ErrUnexpectedEOF, "upstream_truncated"},
		{"eof", io.EOF, "upstream_eof"},
		{"timeout", &net.OpError{Op: "read", Err: &timeoutError{}}, "upstream_timeout"},
		{"unknown", errors.New("something else"), "unknown_error"},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			if got := ErrorCode(testCase.err); got != testCase.want {
				t.Fatalf("ErrorCode(%v) = %q, want %q", testCase.err, got, testCase.want)
			}
		})
	}
}

// TestStatusCodeMapsQuotaToRetryable proves a quota wall reaches the client as
// a rate limit. The upstream answers 400 and every OpenAI-compatible client
// treats 4xx as fatal, so a 429 (plus Retry-After) is what makes an SDK retry
// instead of surfacing a permanent-looking error.
func TestStatusCodeMapsQuotaToRetryable(t *testing.T) {
	resetAt := time.Now().Add(90 * time.Minute)
	cases := []struct {
		name string
		err  error
		want int
	}{
		{"quota is a rate limit", &QuotaExceededError{
			UpstreamError: UpstreamError{StatusCode: 400, Message: "usage limit"},
			ResetAt:       &resetAt,
		}, http.StatusTooManyRequests},
		{"wrapped quota keeps 429", fmt.Errorf("codebuddy: %w", &QuotaExceededError{
			UpstreamError: UpstreamError{StatusCode: 400, Message: "usage limit"},
		}), http.StatusTooManyRequests},
		{"no routes is a 503", fmt.Errorf("glm-5.3: %w", UnavailableError), http.StatusServiceUnavailable},
		{"plain upstream keeps its status", &UpstreamError{StatusCode: 400, Message: "bad request"}, 400},
		// 11128 is "back off and retry later": a 5xx keeps SDKs retrying, while
		// the upstream's own 400 would look permanent.
		{"channel block stays retryable", &ChannelBlockedError{
			UpstreamError{StatusCode: 400, Message: "risk control"},
		}, http.StatusBadGateway},
		{"unclassified is a 502", errors.New("dial tcp: refused"), 502},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			if got := StatusCode(testCase.err); got != testCase.want {
				t.Fatalf("StatusCode(%v) = %d, want %d", testCase.err, got, testCase.want)
			}
		})
	}
}

// TestRetryAfterReportsQuotaReset proves the reset instant the upstream sent is
// exposed as a wait, and that a stale instant still asks for a short pause
// instead of inviting an immediate retry into the same wall.
func TestRetryAfterReportsQuotaReset(t *testing.T) {
	now := time.Now()
	future := now.Add(30 * time.Minute)
	past := now.Add(-5 * time.Minute)

	quota := &QuotaExceededError{
		UpstreamError: UpstreamError{StatusCode: 400, Message: "usage limit"},
		ResetAt:       &future,
	}
	if got := RetryAfter(quota, now); got < 1790 || got > 1800 {
		t.Fatalf("RetryAfter(future) = %d, want ~1800", got)
	}
	stale := &QuotaExceededError{
		UpstreamError: UpstreamError{StatusCode: 400, Message: "usage limit"},
		ResetAt:       &past,
	}
	if got := RetryAfter(stale, now); got != 1 {
		t.Fatalf("RetryAfter(stale) = %d, want 1", got)
	}
	// Errors without a reset instant must not fabricate a wait.
	if got := RetryAfter(&UpstreamError{StatusCode: 400, Message: "x"}, now); got != 0 {
		t.Fatalf("RetryAfter(plain upstream) = %d, want 0", got)
	}
	if got := RetryAfter(UnavailableError, now); got != 0 {
		t.Fatalf("RetryAfter(no routes) = %d, want 0", got)
	}
}

// TestErrorCodeUnwrapsPrecommitFailure proves the pool's failover wrapper does
// not hide the real class: a wrapped upstream error keeps its own code.
func TestErrorCodeUnwrapsPrecommitFailure(t *testing.T) {
	wrapped := &precommitFailure{err: &UpstreamError{StatusCode: 504, Message: "gateway timeout"}}
	if got := ErrorCode(wrapped); got != "upstream_error" {
		t.Fatalf("ErrorCode(precommitFailure) = %q, want upstream_error", got)
	}
}

// TestIsClientAbortSeparatesCallerFaults proves caller-side endings are not
// reported as proxy failures.
func TestIsClientAbortSeparatesCallerFaults(t *testing.T) {
	aborts := []error{
		context.Canceled,
		fmt.Errorf("post \"https://upstream\": %w", context.Canceled),
		syscall.EPIPE,
		syscall.ECONNRESET,
		http.ErrAbortHandler,
	}
	for _, err := range aborts {
		if !IsClientAbort(err) {
			t.Fatalf("IsClientAbort(%v) = false, want true", err)
		}
	}
	faults := []error{
		nil,
		&UpstreamError{StatusCode: 502, Message: "bad gateway"},
		io.ErrUnexpectedEOF,
		UnavailableError,
	}
	for _, err := range faults {
		if IsClientAbort(err) {
			t.Fatalf("IsClientAbort(%v) = true, want false", err)
		}
	}
}

// TestUpstreamClientBoundsHeaderWait proves the transport carries the configured
// response-header ceiling, which is what lets a stalled upstream fail over
// instead of holding the request until the caller gives up.
func TestUpstreamClientBoundsHeaderWait(t *testing.T) {
	client := newUpstreamClient(300 * time.Second)
	transport, ok := client.Transport.(*http.Transport)
	if !ok {
		t.Fatalf("transport is %T, want *http.Transport", client.Transport)
	}
	if transport.ResponseHeaderTimeout != 300*time.Second {
		t.Fatalf("ResponseHeaderTimeout = %v, want 300s", transport.ResponseHeaderTimeout)
	}
	if transport.Proxy != nil {
		t.Fatal("upstream client must ignore proxy environment variables")
	}
	if client.Timeout != 0 {
		t.Fatalf("client.Timeout = %v, want 0 so streams are not cut short", client.Timeout)
	}
}

// timeoutError implements net.Error with Timeout() == true.
type timeoutError struct{}

func (e *timeoutError) Error() string   { return "i/o timeout" }
func (e *timeoutError) Timeout() bool   { return true }
func (e *timeoutError) Temporary() bool { return true }

var _ net.Error = (*timeoutError)(nil)
