// Package checkin implements the domestic WorkBuddy sign-in pipeline.
//
// One upstream client performs the two-step exchange (optional status
// preflight, then claim), and the service drives the batch: it claims a run row,
// walks the eligible accounts, records a per-account attempt, and writes the
// terminal day marker that makes the whole thing idempotent.
package checkin

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// Exact persisted outcome strings.
const (
	OutcomeClaimed          = "CLAIMED"
	OutcomeAlreadyCheckedIn = "ALREADY_CHECKED_IN"
	OutcomeAuthFailed       = "AUTH_FAILED"
	OutcomeNeedsReauth      = "NEEDS_REAUTH"
	OutcomeRateLimited      = "RATE_LIMITED"
	OutcomeTransientError   = "TRANSIENT_ERROR"
	OutcomeFailed           = "FAILED"
	OutcomeSkipped          = "SKIPPED"
)

// alreadyCode is the upstream business code meaning "today is already claimed".
const alreadyCode = "10001"

// Result is one account's check-in attempt.
type Result struct {
	Outcome           string
	Provider          string
	AccountID         string
	HTTPStatus        *int
	BusinessCode      *string
	RequestID         *string
	Message           string
	RewardCredits     *float64
	RewardExpiresAt   *string
	QuotaBefore       map[string]any
	QuotaAfter        map[string]any
	QuotaDelta        map[string]any
	QuotaObservedAt   *string
	QuotaChangeStatus *string
}

// OK reports whether the outcome counts as a success, i.e. the day is claimed.
func (r Result) OK() bool {
	return r.Outcome == OutcomeClaimed || r.Outcome == OutcomeAlreadyCheckedIn
}

// ClientOptions configures the upstream client.
type ClientOptions struct {
	BaseURL      string
	StatusPath   string
	ClaimPath    string
	StatusMethod string
	ClaimMethod  string
	Timeout      time.Duration
}

// Credential is the resolved auth material for one account.
type Credential struct {
	Mode         string
	AccessToken  string
	Cookie       string
	ExtraHeaders map[string]string
}

// Client performs the upstream status and claim calls.
type Client struct {
	options ClientOptions
	http    *http.Client
}

// NewClient builds a client.
func NewClient(options ClientOptions) *Client {
	timeout := options.Timeout
	if timeout <= 0 {
		timeout = 15 * time.Second
	}
	return &Client{
		options: options,
		http: &http.Client{
			// Proxy environment variables are deliberately ignored: the host runs
			// a TUN-mode proxy and a pooled connection through it caused
			// multi-minute stalls in the Python build.
			Transport: &http.Transport{Proxy: nil, MaxIdleConnsPerHost: 8},
			Timeout:   timeout,
			// The upstream must not be followed: a redirect would carry the
			// Authorization header to another origin.
			CheckRedirect: func(*http.Request, []*http.Request) error {
				return http.ErrUseLastResponse
			},
		},
	}
}

// Run performs one sign-in attempt.
func (c *Client) Run(ctx context.Context, accountID string, cred Credential) (Result, error) {
	headers, err := buildHeaders(cred)
	if err != nil {
		return Result{Outcome: OutcomeAuthFailed, Provider: "codebuddy", AccountID: accountID, Message: err.Error()}, nil
	}
	if c.options.StatusMethod != "" {
		if pre := c.status(ctx, accountID, headers); pre != nil {
			return *pre, nil
		}
	}
	return c.claim(ctx, accountID, headers), nil
}

// status runs the optional preflight. A nil result means "not already checked
// in, or the probe could not decide" and the caller falls through to the claim.
func (c *Client) status(ctx context.Context, accountID string, headers map[string]string) *Result {
	response, body, err := c.request(ctx, c.options.StatusMethod, joinURL(c.options.BaseURL, c.options.StatusPath), headers, nil)
	if err != nil {
		// A transport failure must not fail the sign-in: the claim below is the
		// authoritative call, so the probe simply abstains.
		return nil
	}
	requestID := extractRequestID(body, response.Header)
	switch response.StatusCode {
	case http.StatusUnauthorized, http.StatusForbidden, http.StatusTooManyRequests:
		result := classifyHTTPError(accountID, response.StatusCode, body, requestID)
		return &result
	}
	if body != nil && looksAlreadyChecked(body) {
		result := Result{
			Outcome:       OutcomeAlreadyCheckedIn,
			Provider:      "codebuddy",
			AccountID:     accountID,
			HTTPStatus:    intPtr(response.StatusCode),
			BusinessCode:  stringPtr(extractBusinessCode(body)),
			RequestID:     requestID,
			Message:       orDefault(extractMessage(body), "already checked in (status)"),
			RewardCredits: extractReward(body),
		}
		return &result
	}
	return nil
}

// claim performs the authoritative sign-in call.
func (c *Client) claim(ctx context.Context, accountID string, headers map[string]string) Result {
	response, body, err := c.request(ctx, c.options.ClaimMethod, joinURL(c.options.BaseURL, c.options.ClaimPath), headers, []byte("{}"))
	if err != nil {
		return Result{
			Outcome:   OutcomeTransientError,
			Provider:  "codebuddy",
			AccountID: accountID,
			Message:   "transport error: " + typeName(err),
		}
	}
	requestID := extractRequestID(body, response.Header)
	code := extractBusinessCode(body)
	message := extractMessage(body)

	// The upstream answers 400 with business code 10001 when the day is already
	// claimed, so the code outranks the status.
	if code == alreadyCode && (response.StatusCode == http.StatusBadRequest || isSuccess(response.StatusCode)) {
		return Result{
			Outcome:       OutcomeAlreadyCheckedIn,
			Provider:      "codebuddy",
			AccountID:     accountID,
			HTTPStatus:    intPtr(response.StatusCode),
			BusinessCode:  stringPtr(code),
			RequestID:     requestID,
			Message:       orDefault(message, "already checked in"),
			RewardCredits: extractReward(body),
		}
	}
	if isSuccess(response.StatusCode) {
		return Result{
			Outcome:         OutcomeClaimed,
			Provider:        "codebuddy",
			AccountID:       accountID,
			HTTPStatus:      intPtr(response.StatusCode),
			BusinessCode:    stringPtr(code),
			RequestID:       requestID,
			Message:         orDefault(message, "claimed"),
			RewardCredits:   extractReward(body),
			RewardExpiresAt: extractRewardExpiry(body),
		}
	}
	return classifyHTTPError(accountID, response.StatusCode, body, requestID)
}

// request performs one upstream call and reads the body.
func (c *Client) request(ctx context.Context, method, target string, headers map[string]string, payload []byte) (*http.Response, map[string]any, error) {
	var reader io.Reader
	if payload != nil {
		reader = bytes.NewReader(payload)
	}
	request, err := http.NewRequestWithContext(ctx, method, target, reader)
	if err != nil {
		return nil, nil, err
	}
	for key, value := range headers {
		request.Header.Set(key, value)
	}
	response, err := c.http.Do(request)
	if err != nil {
		return nil, nil, err
	}
	defer response.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(response.Body, 64*1024))
	return response, parseJSONBody(raw), nil
}

// buildHeaders renders the per-account auth headers.
//
// Cookie isolation is explicit: only the per-account cookie is sent, never a
// client-level jar, so one account's session can never leak into another's call.
func buildHeaders(cred Credential) (map[string]string, error) {
	headers := map[string]string{"Accept": "application/json", "Content-Type": "application/json"}
	useBearer := cred.Mode == "bearer" || cred.Mode == "bearer_cookie" || cred.Mode == "oauth" || cred.Mode == "inherit_chat"
	useCookie := cred.Mode == "cookie" || cred.Mode == "bearer_cookie"
	if !useBearer && !useCookie {
		return nil, errors.New("unsupported auth_mode: " + cred.Mode)
	}
	if useBearer {
		if cred.AccessToken == "" {
			return nil, errors.New("missing bearer credential")
		}
		headers["Authorization"] = "Bearer " + cred.AccessToken
	}
	if useCookie {
		if cred.Cookie == "" {
			return nil, errors.New("missing cookie credential")
		}
		headers["Cookie"] = cred.Cookie
	}
	for key, value := range cred.ExtraHeaders {
		switch strings.ToLower(key) {
		case "x-user-id", "x-enterprise-id", "x-tenant-id", "x-domain":
			headers[http.CanonicalHeaderKey(key)] = value
		}
	}
	return headers, nil
}

// classifyHTTPError maps a generic HTTP failure to an outcome.
func classifyHTTPError(accountID string, statusCode int, body map[string]any, requestID *string) Result {
	outcome, fallback := OutcomeFailed, "http "+strconv.Itoa(statusCode)
	switch {
	case statusCode == http.StatusUnauthorized || statusCode == http.StatusForbidden:
		outcome, fallback = OutcomeNeedsReauth, "authentication failed"
	case statusCode == http.StatusTooManyRequests:
		outcome, fallback = OutcomeRateLimited, "rate limited"
	case statusCode >= 500:
		outcome, fallback = OutcomeTransientError, "upstream server error"
	}
	return Result{
		Outcome:      outcome,
		Provider:     "codebuddy",
		AccountID:    accountID,
		HTTPStatus:   intPtr(statusCode),
		BusinessCode: stringPtr(extractBusinessCode(body)),
		RequestID:    requestID,
		Message:      orDefault(extractMessage(body), fallback),
	}
}

// looksAlreadyChecked applies the upstream's already-claimed markers.
func looksAlreadyChecked(body map[string]any) bool {
	if extractBusinessCode(body) == alreadyCode {
		return true
	}
	status := strings.ToUpper(orDefault(stringValue(body["status"]), stringValue(body["state"])))
	switch status {
	case "CHECKED_IN", "ALREADY_CHECKED_IN", "CLAIMED", "CLAIMED_TODAY", "DONE":
		return true
	}
	if truthy(body["checkedIn"]) || truthy(body["checked_in"]) {
		return true
	}
	if isFalse(body["canCheckIn"]) || isFalse(body["can_check_in"]) {
		return truthy(body["alreadyCheckedIn"]) || truthy(body["already_checked_in"])
	}
	return false
}

func parseJSONBody(raw []byte) map[string]any {
	text := strings.TrimSpace(string(raw))
	if text == "" {
		return nil
	}
	var out map[string]any
	if err := json.Unmarshal([]byte(text), &out); err != nil {
		return nil
	}
	return out
}

func extractBusinessCode(body map[string]any) string {
	if body == nil {
		return ""
	}
	for _, key := range []string{"code", "businessCode", "business_code", "errCode", "error_code"} {
		if value, ok := body[key]; ok && value != nil {
			switch typed := value.(type) {
			case string:
				return typed
			case float64:
				return strconv.FormatFloat(typed, 'f', -1, 64)
			}
		}
	}
	return ""
}

func extractRequestID(body map[string]any, headers http.Header) *string {
	for _, key := range []string{"requestId", "request_id", "reqId"} {
		if body != nil {
			if value := stringValue(body[key]); value != "" {
				return &value
			}
		}
	}
	for _, key := range []string{"X-Request-Id", "X-Requestid", "Request-Id"} {
		if value := headers.Get(key); value != "" {
			return &value
		}
	}
	return nil
}

func extractMessage(body map[string]any) string {
	if body == nil {
		return ""
	}
	for _, key := range []string{"msg", "message", "error", "detail"} {
		if value := stringValue(body[key]); value != "" {
			return truncate(value, 200)
		}
	}
	return ""
}

// extractReward reads the credited amount across the wrapper shapes the upstream
// uses (the value may sit on the body or one level down).
func extractReward(body map[string]any) *float64 {
	if body == nil {
		return nil
	}
	for _, key := range []string{"rewardCredits", "reward_credits", "reward", "credits"} {
		if value, ok := numericValue(body[key]); ok {
			return &value
		}
	}
	for _, wrapper := range []string{"data", "Data", "response", "Response"} {
		nested, ok := body[wrapper].(map[string]any)
		if !ok {
			continue
		}
		for _, key := range []string{"rewardCredits", "reward_credits", "reward", "credits"} {
			if value, ok := numericValue(nested[key]); ok {
				return &value
			}
		}
	}
	return nil
}

func extractRewardExpiry(body map[string]any) *string {
	if body == nil {
		return nil
	}
	for _, key := range []string{"rewardExpiresAt", "reward_expires_at", "expiresAt"} {
		if value := stringValue(body[key]); value != "" {
			return &value
		}
	}
	return nil
}

func joinURL(base, path string) string {
	base = strings.TrimRight(base, "/")
	if path == "" {
		return base
	}
	if !strings.HasPrefix(path, "/") {
		path = "/" + path
	}
	return base + path
}

// BuildAuthURL is exported for the probe path, which needs the same join rule.
func BuildAuthURL(base, path string) string { return joinURL(base, path) }

// EscapeQuery keeps a value safe for a query string.
func EscapeQuery(value string) string { return url.QueryEscape(value) }

func isSuccess(status int) bool { return status >= 200 && status < 300 }

func truthy(value any) bool {
	switch typed := value.(type) {
	case bool:
		return typed
	case string:
		return strings.EqualFold(typed, "true")
	}
	return false
}

func isFalse(value any) bool {
	if flag, ok := value.(bool); ok {
		return !flag
	}
	return false
}

func stringValue(value any) string {
	switch typed := value.(type) {
	case string:
		return typed
	case float64:
		return strconv.FormatFloat(typed, 'f', -1, 64)
	case bool:
		return strconv.FormatBool(typed)
	}
	return ""
}

func numericValue(value any) (float64, bool) {
	switch typed := value.(type) {
	case float64:
		return typed, true
	case int:
		return float64(typed), true
	case string:
		parsed, err := strconv.ParseFloat(typed, 64)
		if err != nil {
			return 0, false
		}
		return parsed, true
	}
	return 0, false
}

// typeName renders an error's dynamic type without its package qualifier, so a
// transport failure reads as "transport error: url.Error" rather than the full
// package path.
func typeName(err error) string {
	if err == nil {
		return ""
	}
	name := fmt.Sprintf("%T", err)
	if index := strings.LastIndex(name, "."); index >= 0 {
		return name[index+1:]
	}
	return strings.TrimPrefix(name, "*")
}

func truncate(value string, limit int) string {
	if len(value) <= limit {
		return value
	}
	return value[:limit]
}

func orDefault(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}

func intPtr(value int) *int { return &value }

func stringPtr(value string) *string {
	if value == "" {
		return nil
	}
	return &value
}
