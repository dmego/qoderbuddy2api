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
