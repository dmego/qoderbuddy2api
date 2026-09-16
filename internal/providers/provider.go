// Package providers implements the upstream WorkBuddy chat providers and the
// pool/router layer in front of them.
package providers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"regexp"
	"strings"
	"syscall"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/chatwire"
)

// UpstreamError is an upstream failure with the HTTP status the client should
// see. The type is named UpstreamError rather than Error because the embedded
// field in the specialised errors below would otherwise collide with the
// Error() method.
type UpstreamError struct {
	StatusCode int
	Message    string
}

func (e *UpstreamError) Error() string {
	return fmt.Sprintf("workbuddy %d: %s", e.StatusCode, e.Message)
}

// ChannelBlockedError is upstream risk control rejecting the channel (11128).
type ChannelBlockedError struct{ UpstreamError }

// QuotaExceededError is a per-account, per-model usage limit (6004). The reset
// instant is parsed from the upstream message so the pool can block the pair
// until then instead of retrying into the same wall.
type QuotaExceededError struct {
	UpstreamError
	ResetAt *time.Time
}

// UnavailableError means no route could serve the request at all.
var UnavailableError = errors.New("no available routes")

const (
	channelBlockedCode = "11128"
	quotaExceededCode  = "6004"
)

// resetAtPattern matches «将在 2026-09-11 15:00:00 UTC+8 重置».
var resetAtPattern = regexp.MustCompile(`将在\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*UTC\+8`)

var resetZone = time.FixedZone("UTC+8", 8*3600)

// ParseUpstreamError converts an upstream error body into a typed error.
func ParseUpstreamError(statusCode int, body string) error {
	clean := strings.TrimSpace(strings.ReplaceAll(body, "\n", " "))
	var payload struct {
		Code       any    `json:"code"`
		Msg        string `json:"msg"`
		DisplayMsg struct {
			En string `json:"en"`
		} `json:"displayMsg"`
	}
	if err := json.Unmarshal([]byte(body), &payload); err != nil {
		return &UpstreamError{StatusCode: statusCode, Message: truncate(clean, 200)}
	}
	code := codeString(payload.Code)
	message := payload.Msg
	if message == "" {
		message = truncate(clean, 200)
	}
	switch code {
	case channelBlockedCode:
		detail := message
		if payload.DisplayMsg.En != "" {
			detail = message + "; " + payload.DisplayMsg.En
		}
		return &ChannelBlockedError{UpstreamError{
			StatusCode: statusCode,
			Message:    detail + " — upstream security policy; back off and retry later",
		}}
	case quotaExceededCode:
		return &QuotaExceededError{
			UpstreamError: UpstreamError{StatusCode: statusCode, Message: message},
			ResetAt:       parseResetAt(message),
		}
	}
	return &UpstreamError{StatusCode: statusCode, Message: truncate(clean, 200)}
}

// ResetAt reports the model-block deadline carried by a quota error, if any.
func ResetAt(err error) *time.Time {
	var quota *QuotaExceededError
	if errors.As(err, &quota) {
		return quota.ResetAt
	}
	return nil
}

// StatusCode extracts the HTTP status carried by an upstream error.
func StatusCode(err error) int {
	var upstream *UpstreamError
	if errors.As(err, &upstream) {
		return upstream.StatusCode
	}
	return 502
}

// ErrorCode renders a stable, low-cardinality classification of a failed
// request for telemetry.
//
// The recorder used to store the Go type name (`*errors.errorString`,
// `*fmt.wrapError`), which collapsed every distinct failure into one
// uninformative value — a wrapped transport error is always `*fmt.wrapError`.
// The classification is deliberately stable: the console and the CSV export
// group by it, so new members are added, never renamed.
func ErrorCode(err error) string {
	if err == nil {
		return ""
	}
	switch {
	case errors.Is(err, context.Canceled):
		return "client_canceled"
	case errors.Is(err, context.DeadlineExceeded):
		return "deadline_exceeded"
	case errors.Is(err, UnavailableError):
		return "no_available_routes"
	}
	var blocked *ChannelBlockedError
	if errors.As(err, &blocked) {
		return "channel_blocked"
	}
	var quota *QuotaExceededError
	if errors.As(err, &quota) {
		return "quota_exceeded"
	}
	var upstream *UpstreamError
	if errors.As(err, &upstream) {
		return "upstream_error"
	}
	// Transport-level failures, checked last so a typed upstream error above
	// always wins over the generic net.Error it may wrap.
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return "upstream_timeout"
	}
	switch {
	case errors.Is(err, io.ErrUnexpectedEOF):
		return "upstream_truncated"
	case errors.Is(err, io.EOF):
		return "upstream_eof"
	}
	return "unknown_error"
}

// IsClientAbort reports whether a request ended because the caller went away or
// gave up, rather than because of an upstream or proxy fault. Such a request is
// neither a success nor a failure: the client stopped listening, so the proxy
// cannot claim the outcome.
func IsClientAbort(err error) bool {
	return errors.Is(err, context.Canceled) ||
		errors.Is(err, syscall.EPIPE) ||
		errors.Is(err, syscall.ECONNRESET) ||
		errors.Is(err, http.ErrAbortHandler)
}

func parseResetAt(message string) *time.Time {
	match := resetAtPattern.FindStringSubmatch(message)
	if match == nil {
		return nil
	}
	parsed, err := time.ParseInLocation("2006-01-02 15:04:05", match[1], resetZone)
	if err != nil {
		return nil
	}
	utc := parsed.UTC()
	return &utc
}

func codeString(value any) string {
	switch typed := value.(type) {
	case string:
		return typed
	case float64:
		return fmt.Sprintf("%.0f", typed)
	case json.Number:
		return typed.String()
	}
	return ""
}

func truncate(value string, limit int) string {
	if len(value) <= limit {
		return value
	}
	return value[:limit]
}

// Provider is one upstream chat backend.
type Provider interface {
	Name() string
	// Complete performs a non-streaming completion, aggregated from upstream SSE.
	Complete(ctx context.Context, request *chatwire.ChatRequest) (map[string]any, error)
	// Stream performs a streaming completion, emitting OpenAI-framed SSE frames.
	Stream(ctx context.Context, request *chatwire.ChatRequest) (FrameStream, error)
	Close() error
}

// FrameStream yields downstream-ready SSE frames. Next returns io.EOF when done.
type FrameStream interface {
	Next() ([]byte, error)
	Close() error
}
