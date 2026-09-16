package oauthflow

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"time"
)

// pendingCode is the upstream "user has not finished signing in yet" marker.
const pendingCode = 11217

// authStart is the minted state plus the console URL to open.
type authStart struct {
	State   string
	AuthURL string
}

// pollResult is one poll outcome.
type pollResult struct {
	Status       string
	Code         int
	AccessToken  string
	RefreshToken string
	ExpiresIn    int
	Message      string
}

// client performs the plugin auth calls.
type client struct {
	http *http.Client
}

func newClient() *client {
	return &client{http: &http.Client{
		// Proxy environment variables are deliberately ignored: the deployment
		// host runs a TUN-mode proxy, and a pooled connection through it cost
		// 200+ second stalls in the Python build.
		Transport: &http.Transport{Proxy: nil, MaxIdleConnsPerHost: 8},
		Timeout:   20 * time.Second,
	}}
}

// start mints a login state from the provider.
func (c *client) start(ctx context.Context, endpoint, domain string) (authStart, error) {
	nonce := randomToken(16)
	url := endpoint + "/v2/plugin/auth/state?platform=CLI&nonce=" + nonce
	body, _ := json.Marshal(map[string]any{"nonce": nonce})
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return authStart{}, err
	}
	for key, value := range startHeaders(domain) {
		request.Header.Set(key, value)
	}
	response, err := c.http.Do(request)
	if err != nil {
		return authStart{}, errors.New("auth_start_request_failed")
	}
	defer response.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(response.Body, 64*1024))
	if response.StatusCode != http.StatusOK {
		return authStart{}, errors.New("auth_start_http_error")
	}
	var envelope struct {
		Code int             `json:"code"`
		Data json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return authStart{}, errors.New("auth_start_http_error")
	}
	if envelope.Code != 0 {
		return authStart{}, errors.New("auth_start_failed")
	}
	var data struct {
		State    string `json:"state"`
		AuthURL  string `json:"authUrl"`
		AuthURL2 string `json:"auth_url"`
	}
	if err := json.Unmarshal(envelope.Data, &data); err != nil {
		return authStart{}, errors.New("auth_start_failed")
	}
	authURL := data.AuthURL
	if authURL == "" {
		authURL = data.AuthURL2
	}
	if data.State == "" || authURL == "" {
		return authStart{}, errors.New("auth_start_missing_fields")
	}
	return authStart{State: data.State, AuthURL: authURL}, nil
}

// poll asks the provider whether the sign-in finished.
func (c *client) poll(ctx context.Context, endpoint, domain, state string) (pollResult, error) {
	if state == "" {
		return pollResult{Status: "error", Message: "missing_state"}, nil
	}
	url := endpoint + "/v2/plugin/auth/token?state=" + state
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return pollResult{}, err
	}
	for key, value := range pollHeaders(domain) {
		request.Header.Set(key, value)
	}
	response, err := c.http.Do(request)
	if err != nil {
		return pollResult{Status: "error", Message: "auth_poll_request_failed"}, nil
	}
	defer response.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(response.Body, 64*1024))
	return parsePoll(raw), nil
}

// parsePoll interprets the provider's poll envelope.
func parsePoll(raw []byte) pollResult {
	var envelope struct {
		Code int             `json:"code"`
		Msg  string          `json:"msg"`
		Data json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return pollResult{Status: "error", Message: "auth_poll_invalid_json"}
	}
	if envelope.Code == pendingCode {
		return pollResult{Status: "pending", Code: pendingCode, Message: "waiting"}
	}
	if envelope.Code != 0 || len(envelope.Data) == 0 {
		message := envelope.Msg
		if message == "" {
			message = "auth_poll_failed"
		}
		return pollResult{Status: "error", Code: envelope.Code, Message: truncate(message, 200)}
	}
	var data struct {
		AccessToken    string `json:"accessToken"`
		AccessTokenAlt string `json:"access_token"`
		RefreshToken   string `json:"refreshToken"`
		RefreshAlt     string `json:"refresh_token"`
		ExpiresIn      any    `json:"expiresIn"`
		ExpiresInAlt   any    `json:"expires_in"`
	}
	if err := json.Unmarshal(envelope.Data, &data); err != nil {
		return pollResult{Status: "error", Message: "auth_poll_invalid_json"}
	}
	access := firstNonEmpty(data.AccessToken, data.AccessTokenAlt)
	if access == "" {
		return pollResult{Status: "error", Code: 0, Message: "auth_poll_no_token"}
	}
	expiresIn := 0
	if data.ExpiresIn != nil {
		expiresIn = toInt(data.ExpiresIn)
	} else {
		expiresIn = toInt(data.ExpiresInAlt)
	}
	return pollResult{
		Status:       "success",
		Code:         0,
		AccessToken:  access,
		RefreshToken: firstNonEmpty(data.RefreshToken, data.RefreshAlt),
		ExpiresIn:    expiresIn,
	}
}

func toInt(value any) int {
	switch typed := value.(type) {
	case float64:
		return int(typed)
	case int:
		return typed
	case string:
		parsed, err := strconv.Atoi(typed)
		if err != nil {
			return 0
		}
		return parsed
	}
	return 0
}

// startHeaders are the headers the state endpoint expects.
func startHeaders(domain string) map[string]string {
	return map[string]string{
		"Accept":               "application/json, text/plain, */*",
		"Content-Type":         "application/json",
		"Cache-Control":        "no-cache",
		"Pragma":               "no-cache",
		"X-Requested-With":     "XMLHttpRequest",
		"X-Domain":             domain,
		"X-No-Authorization":   "true",
		"X-No-User-Id":         "true",
		"X-No-Enterprise-Id":   "true",
		"X-No-Department-Info": "true",
		"User-Agent":           "CLI/1.0.8 CodeBuddy/1.0.8",
		"X-Product":            "SaaS",
		"X-Request-ID":         randomTokenHex(16),
	}
}

// pollHeaders are the headers the token endpoint expects, including the B3
// trace triple the gateway validates.
func pollHeaders(domain string) map[string]string {
	traceID := randomTokenHex(16)
	spanID := randomTokenHex(8)
	return map[string]string{
		"Accept":               "application/json, text/plain, */*",
		"Cache-Control":        "no-cache",
		"Pragma":               "no-cache",
		"X-Requested-With":     "XMLHttpRequest",
		"X-Request-ID":         traceID,
		"b3":                   traceID + "-" + spanID + "-1-",
		"X-B3-TraceId":         traceID,
		"X-B3-ParentSpanId":    "",
		"X-B3-SpanId":          spanID,
		"X-B3-Sampled":         "1",
		"X-No-Authorization":   "true",
		"X-No-User-Id":         "true",
		"X-No-Enterprise-Id":   "true",
		"X-No-Department-Info": "true",
		"X-Domain":             domain,
		"User-Agent":           "CLI/1.0.8 CodeBuddy/1.0.8",
		"X-Product":            "SaaS",
	}
}

// hashToken renders the sha256 hex digest used to address a flow by state.
func hashToken(token string) string {
	digest := sha256.Sum256([]byte(token))
	return hex.EncodeToString(digest[:])
}

func randomToken(bytes int) string {
	raw := make([]byte, bytes)
	if _, err := rand.Read(raw); err != nil {
		return strconv.FormatInt(time.Now().UnixNano(), 36)
	}
	return base64.RawURLEncoding.EncodeToString(raw)
}

func randomTokenHex(bytes int) string {
	raw := make([]byte, bytes)
	if _, err := rand.Read(raw); err != nil {
		return hex.EncodeToString([]byte(strconv.FormatInt(time.Now().UnixNano(), 10)))
	}
	return hex.EncodeToString(raw)
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if value != "" {
			return value
		}
	}
	return ""
}

func truncate(value string, limit int) string {
	if len(value) <= limit {
		return value
	}
	return value[:limit]
}
