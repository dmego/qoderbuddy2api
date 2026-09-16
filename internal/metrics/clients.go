package metrics

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

	"github.com/dmego/qoderbuddy2api/internal/config"
	"github.com/dmego/qoderbuddy2api/internal/models"
	"github.com/dmego/qoderbuddy2api/internal/store"
)

// maxResponseBytes bounds one upstream read. Credit payloads carry a package
// list, so they are larger than the other responses, but a misbehaving endpoint
// must not be able to stream unbounded data into memory.
const maxResponseBytes = 4 << 20

// browserUserAgent is the Chrome desktop identity the billing endpoints expect.
// They answer 403 to the default Go agent.
const browserUserAgent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
	"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

// creditClient performs the real upstream reads for both kept providers.
type creditClient struct {
	settings config.Settings
	http     *http.Client
}

// NewCreditClients builds the production ProviderClients.
func NewCreditClients(settings config.Settings) ProviderClients {
	return &creditClient{settings: settings, http: newUpstreamClient(settings)}
}

// newUpstreamClient builds the HTTP client for the billing endpoints.
//
// Proxy environment variables are deliberately ignored: this process runs next
// to a TUN-mode proxy on the developer's machine, and a half-dead pooled
// connection through it cost 200+ second stalls on non-streaming requests.
func newUpstreamClient(settings config.Settings) *http.Client {
	timeout := settings.CheckinTimeout
	if timeout <= 0 {
		timeout = 15
	}
	return &http.Client{
		Transport: &http.Transport{
			Proxy:                 nil,
			MaxIdleConns:          16,
			MaxIdleConnsPerHost:   4,
			IdleConnTimeout:       90 * time.Second,
			TLSHandshakeTimeout:   10 * time.Second,
			ExpectContinueTimeout: 1 * time.Second,
			ForceAttemptHTTP2:     true,
		},
		Timeout: time.Duration(timeout) * time.Second,
	}
}

// FetchCredits reads one account's credit balance.
func (c *creditClient) FetchCredits(ctx context.Context, provider, _ string, cred Credential) (map[string]any, error) {
	base, path, ok := c.creditEndpoint(provider)
	if !ok {
		return nil, fmt.Errorf("no credit endpoint for provider %s", provider)
	}
	if cred.AccessToken == "" {
		return nil, errors.New("access credential unavailable")
	}
	body, err := c.postJSON(ctx, base, path, cred)
	if err != nil {
		return nil, err
	}
	value := normalizeCredits(body)
	if len(value) == 0 {
		return nil, errors.New("empty credits response")
	}
	return value, nil
}

// FetchCheckinStatus reads one account's upstream check-in state.
//
// The domestic preflight is disabled unless an operator sets the status method
// (the same switch the check-in pipeline honours); with it unset the call is
// skipped, and the check-in metric is then served from checkin_daily_state
// alone. The international deployment has no check-in endpoint at all.
func (c *creditClient) FetchCheckinStatus(ctx context.Context, provider, _ string, cred Credential) (map[string]any, error) {
	if provider != models.ProviderWorkBuddy {
		return nil, nil
	}
	method := strings.ToUpper(strings.TrimSpace(c.settings.CodeBuddyCheckinStatusMethod))
	if method == "" {
		return nil, nil
	}
	base := c.settings.CodeBuddyCheckinBase
	if cred.AccessToken == "" && cred.Cookie == "" {
		return nil, errors.New("access credential unavailable")
	}
	body, err := c.do(ctx, method, base, c.settings.CodeBuddyCheckinStatusPath, cred, nil)
	if err != nil {
		return nil, err
	}
	return checkinSummary(body), nil
}

// creditEndpoint resolves the credit read target for a provider.
func (c *creditClient) creditEndpoint(provider string) (base, path string, ok bool) {
	switch provider {
	case models.ProviderWorkBuddy:
		return c.settings.CodeBuddyCheckinBase, c.settings.CodeBuddyCreditsPath, true
	case models.ProviderWorkBuddyIntl:
		return c.settings.WorkBuddyIntlEndpoint, c.settings.WorkBuddyIntlCreditsPath, true
	}
	return "", "", false
}

// postJSON posts an empty JSON object, which is the shape the billing endpoint
// requires.
func (c *creditClient) postJSON(ctx context.Context, base, path string, cred Credential) (map[string]any, error) {
	return c.do(ctx, http.MethodPost, base, path, cred, []byte("{}"))
}

// do performs one upstream request and decodes a JSON object response.
func (c *creditClient) do(ctx context.Context, method, base, path string, cred Credential, body []byte) (map[string]any, error) {
	request, err := http.NewRequestWithContext(ctx, method, joinURL(base, path), bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("transport:%s", errorName(err))
	}
	for name, value := range requestHeaders(base, cred) {
		request.Header.Set(name, value)
	}
	response, err := c.http.Do(request)
	if err != nil {
		return nil, fmt.Errorf("transport:%s", errorName(err))
	}
	defer response.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(response.Body, maxResponseBytes))
	if err != nil {
		return nil, fmt.Errorf("transport:%s", errorName(err))
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		// The status alone is the diagnostic; the body can carry identifiers
		// and is never persisted or logged.
		return nil, fmt.Errorf("http:%d", response.StatusCode)
	}
	return decodeObject(raw), nil
}

// requestHeaders builds the browser-like header set the billing endpoints
// require, plus the per-account auth material.
func requestHeaders(base string, cred Credential) map[string]string {
	origin := originOf(base)
	headers := map[string]string{
		"Accept":            "application/json, text/plain, */*",
		"Content-Type":      "application/json",
		"X-Client-Platform": "web",
		"User-Agent":        browserUserAgent,
		"Origin":            origin,
		"Referer":           origin + "/",
	}
	if cred.AccessToken != "" {
		headers["Authorization"] = "Bearer " + cred.AccessToken
	}
	if cred.Cookie != "" {
		headers["Cookie"] = cred.Cookie
	}
	return headers
}

// originOf reduces a base URL to its scheme and host, which is what a browser
// puts in Origin and Referer.
func originOf(base string) string {
	parsed, err := url.Parse(strings.TrimSpace(base))
	if err != nil || parsed.Scheme == "" || parsed.Host == "" {
		return strings.TrimRight(strings.TrimSpace(base), "/")
	}
	return parsed.Scheme + "://" + parsed.Host
}

// joinURL appends a path to a base URL without doubling the separator.
func joinURL(base, path string) string {
	return strings.TrimRight(strings.TrimSpace(base), "/") + "/" + strings.TrimLeft(path, "/")
}

// decodeObject decodes a JSON object, returning nil for anything else.
func decodeObject(raw []byte) map[string]any {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 {
		return nil
	}
	var out map[string]any
	if err := json.Unmarshal(trimmed, &out); err != nil {
		return nil
	}
	return out
}

// errorName renders a transport failure's Go type, matching the Python
// client's `transport:{ExcName}` classification.
func errorName(err error) string {
	var urlErr *url.Error
	if errors.As(err, &urlErr) && urlErr.Err != nil {
		return fmt.Sprintf("%T", urlErr.Err)
	}
	return fmt.Sprintf("%T", err)
}

// checkinSummary keeps the scalar status fields of a check-in status response.
//
// Only status-bearing scalars survive: the console renders these values, and a
// wider copy would persist parts of the upstream body that the metric has no
// use for.
func checkinSummary(body map[string]any) map[string]any {
	out := map[string]any{}
	for _, name := range []string{"status", "state", "checkedIn", "checked_in", "alreadyCheckedIn", "already_checked_in", "canCheckIn", "can_check_in"} {
		if value, ok := body[name]; ok && isScalar(value) {
			out[name] = value
		}
	}
	if code, ok := businessCode(body); ok {
		out["business_code"] = code
	}
	return out
}

// businessCode finds the upstream business code, which the check-in pipeline
// treats as the authoritative already-claimed marker.
func businessCode(body map[string]any) (string, bool) {
	for _, name := range []string{"code", "businessCode", "business_code", "errCode", "error_code"} {
		value, ok := body[name]
		if !ok || !isScalar(value) {
			continue
		}
		switch typed := value.(type) {
		case string:
			return typed, true
		case float64:
			return strconv.FormatFloat(typed, 'f', -1, 64), true
		case bool:
			continue
		}
	}
	return "", false
}

func isScalar(value any) bool {
	switch value.(type) {
	case string, float64, bool:
		return true
	}
	return false
}

// normalizeCredits returns a bounded, secret-free aggregate of a credits
// response, or nil when the response carries no usable balance.
func normalizeCredits(body map[string]any) map[string]any {
	if body == nil {
		return nil
	}
	if code, ok := body["code"]; !ok || !isZeroCode(code) {
		return nil
	}
	data, _ := body["data"].(map[string]any)
	response, _ := data["Response"].(map[string]any)
	payload, _ := response["Data"].(map[string]any)
	accounts, _ := payload["Accounts"].([]any)
	if len(accounts) == 0 {
		return nil
	}
	return aggregateCredits(accounts)
}

func isZeroCode(code any) bool {
	switch typed := code.(type) {
	case float64:
		return typed == 0
	case string:
		return strings.TrimSpace(typed) == "0"
	}
	return false
}

// aggregateCredits folds the per-package rows into the scalar totals and the
// package list the credits page renders.
//
// The package list is built as []any, the same shape the JSON decoder produces
// when the snapshot is read back. Keeping one canonical representation is what
// lets historyValue strip the array regardless of whether the payload came
// straight from an upstream read or from storage.
func aggregateCredits(accounts []any) map[string]any {
	var totalRemaining, totalUsed, totalCapacity, cycleRemaining, cycleCapacity, depleted int
	lowest := 0
	lowestSet := false
	unit := ""
	var expiries []int64
	packages := make([]any, 0, len(accounts))
	for _, entry := range accounts {
		account, ok := entry.(map[string]any)
		if !ok {
			continue
		}
		summary := summarizeAccount(account, len(packages)+1)
		if unit == "" {
			unit = summary.unit
		}
		totalRemaining += summary.remaining
		totalUsed += summary.used
		totalCapacity += summary.total
		cycleRemaining += summary.cycleRemaining
		cycleCapacity += summary.cycleCapacity
		if summary.remaining <= 0 {
			depleted++
		}
		if !lowestSet || summary.remaining < lowest {
			lowest, lowestSet = summary.remaining, true
		}
		if summary.expiryMS != nil {
			expiries = append(expiries, *summary.expiryMS)
		}
		packages = append(packages, summary.packageRow)
	}
	if unit == "" {
		unit = "credits"
	}
	var expiresAt any
	if len(expiries) > 0 {
		expiresAt = epochMSToISO(earliest(expiries))
	}
	return map[string]any{
		"unit":              unit,
		"total_remaining":   totalRemaining,
		"total_used":        totalUsed,
		"total_capacity":    totalCapacity,
		"cycle_remaining":   cycleRemaining,
		"cycle_capacity":    cycleCapacity,
		"package_count":     len(accounts),
		"depleted_packages": depleted,
		"lowest_remaining":  lowest,
		"expires_at":        expiresAt,
		"packages":          packages,
	}
}

// accountSummary is one upstream package row reduced to the rendered fields.
type accountSummary struct {
	unit           string
	remaining      int
	used           int
	total          int
	cycleRemaining int
	cycleCapacity  int
	expiryMS       *int64
	packageRow     map[string]any
}

// summarizeAccount maps one upstream package row onto the console's field names.
func summarizeAccount(account map[string]any, index int) accountSummary {
	unit, _ := account["CapacityUnit"].(string)
	if unit == "" {
		unit = "credits"
	}
	summary := accountSummary{
		unit:           unit,
		remaining:      number(account["CapacityRemain"]),
		used:           number(account["CapacityUsed"]),
		total:          number(account["CapacitySize"]),
		cycleRemaining: number(account["CycleCapacityRemain"]),
		cycleCapacity:  number(account["CycleCapacitySize"]),
	}
	// WorkBuddy leaves ExpiredTime empty for an active package; the expiry its
	// own usage page shows is CycleEndTime.
	summary.expiryMS = epochMS(account["ExpiredTime"])
	if summary.expiryMS == nil {
		summary.expiryMS = epochMS(account["CycleEndTime"])
	}
	name, _ := account["PackageName"].(string)
	if strings.TrimSpace(name) == "" {
		name, _ = account["ProductName"].(string)
	}
	if strings.TrimSpace(name) == "" {
		name = fmt.Sprintf("积分包 %d", index)
	}
	row := map[string]any{
		"name":      strings.TrimSpace(name),
		"remaining": summary.remaining,
		"used":      summary.used,
		"total":     summary.total,
		"unit":      unit,
	}
	if summary.expiryMS != nil {
		row["expires_at"] = epochMSToISO(*summary.expiryMS)
	}
	summary.packageRow = row
	return summary
}

// number coerces an upstream value to an integer, yielding 0 for booleans and
// anything unparsable so a malformed row cannot blank the totals.
func number(value any) int {
	switch typed := value.(type) {
	case float64:
		return int(typed)
	case string:
		if parsed, err := strconv.ParseFloat(strings.TrimSpace(typed), 64); err == nil {
			return int(parsed)
		}
	}
	return 0
}

// epochMS coerces an upstream timestamp to epoch milliseconds. Naive values are
// read as UTC, matching how the upstream renders them.
func epochMS(value any) *int64 {
	switch typed := value.(type) {
	case float64:
		if typed == 0 {
			return nil
		}
		millis := int64(typed)
		return &millis
	case string:
		text := strings.TrimSpace(typed)
		if text == "" {
			return nil
		}
		if parsed, err := strconv.ParseFloat(text, 64); err == nil {
			millis := int64(parsed)
			return &millis
		}
		instant, ok := store.ParseISO(text)
		if !ok {
			return nil
		}
		millis := instant.UnixMilli()
		return &millis
	}
	return nil
}

// epochMSToISO renders an epoch-millisecond instant in the stored timestamp
// format so history and snapshot values compare consistently.
func epochMSToISO(millis int64) string {
	return store.FormatISO(time.UnixMilli(millis))
}

func earliest(values []int64) int64 {
	minimum := values[0]
	for _, value := range values[1:] {
		if value < minimum {
			minimum = value
		}
	}
	return minimum
}
