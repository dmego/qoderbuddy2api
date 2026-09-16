// Package growth implements the domestic WorkBuddy (provider "codebuddy")
// growth-centre automation: the growth REST client, the active-day ACP client,
// the step orchestrator and its scheduler.
package growth

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/config"
)

// Step keys. These are the exact key set of one automation run's result map,
// the allowed {step} path values and the frontend StepKey union.
const (
	StepTasks     = "tasks"
	StepLottery   = "lottery"
	StepTravel    = "travel"
	StepRedeem    = "redeem"
	StepBuddyOpen = "buddy_open"
	StepActiveDay = "active_day"
)

// StepKeys is the canonical step order.
var StepKeys = []string{StepTasks, StepLottery, StepTravel, StepRedeem, StepBuddyOpen, StepActiveDay}

// IsStep reports whether name is one of the six automation steps.
func IsStep(name string) bool {
	for _, key := range StepKeys {
		if key == name {
			return true
		}
	}
	return false
}

// StepResult is one step's outcome, keyed by step name in the log payload.
// Detail carries the operator-facing text; Error carries a machine code when
// the step failed before producing a comparable detail string.
//
// Extra holds the per-step counters the Python build stored inline (accepted,
// claimed, drawn, available, remaining_days, required_days, tier, opened,
// reward_credits). The console renders only status/detail, but the counters are
// kept because the history payload is operator-visible and the scheduler sums
// reward_credits to decide whether a metrics refresh is warranted.
type StepResult struct {
	Status string         `json:"status"`
	Detail string         `json:"detail,omitempty"`
	Error  string         `json:"error,omitempty"`
	Extra  map[string]any `json:"-"`
}

// MarshalJSON emits status/detail/error plus the counters inline, reproducing
// the flat dict shape the Python build wrote into growth_automation_log.
func (r StepResult) MarshalJSON() ([]byte, error) {
	payload := r.Map()
	encoded, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	return encoded, nil
}

// Map renders the step result as the flat key/value shape the log and the admin
// routes expose. The Python payloads always carried "detail", so it is emitted
// even when empty.
func (r StepResult) Map() map[string]any {
	payload := make(map[string]any, len(r.Extra)+4)
	payload["status"] = r.Status
	payload["detail"] = r.Detail
	if r.Error != "" {
		payload["error"] = r.Error
	}
	for key, value := range r.Extra {
		payload[key] = value
	}
	return payload
}

// UnmarshalJSON tolerates the flat payload shape: the three named keys plus any
// counter the producing build wrote.
func (r *StepResult) UnmarshalJSON(data []byte) error {
	var payload map[string]any
	if err := json.Unmarshal(data, &payload); err != nil {
		return err
	}
	r.Status = stringField(payload["status"])
	r.Detail = stringField(payload["detail"])
	r.Error = stringField(payload["error"])
	delete(payload, "status")
	delete(payload, "detail")
	delete(payload, "error")
	if len(payload) > 0 {
		r.Extra = payload
	}
	return nil
}

// WithExtra attaches counters and returns the result for chaining.
func (r StepResult) WithExtra(values map[string]any) StepResult {
	if len(values) == 0 {
		return r
	}
	if r.Extra == nil {
		r.Extra = make(map[string]any, len(values))
	}
	for key, value := range values {
		r.Extra[key] = value
	}
	return r
}

// Credits reads the reward credit total this step earned, if any.
func (r StepResult) Credits() int {
	return numericInt(r.Extra["reward_credits"])
}

// skippedResult is the placeholder every step starts with.
func skippedResult() StepResult {
	return StepResult{Status: "skipped", Detail: "未启用"}
}

// stepResultsPayload renders a whole run for persistence.
func stepResultsPayload(results map[string]StepResult) map[string]any {
	payload := make(map[string]any, len(results))
	for key, result := range results {
		payload[key] = result.Map()
	}
	return payload
}

// GrowthUnavailableError marks an upstream growth-endpoint failure. Its message
// is a safe code the automation copies straight into a step detail.
type GrowthUnavailableError struct {
	Code string
}

func (e *GrowthUnavailableError) Error() string { return e.Code }

func growthError(code string) error { return &GrowthUnavailableError{Code: code} }

// unavailableCode renders an error the way the Python f-string did: the code for
// upstream failures, otherwise the Go error type name.
func unavailableCode(err error) string {
	var target *GrowthUnavailableError
	if errors.As(err, &target) {
		return target.Code
	}
	return fmt.Sprintf("%T", err)
}

// Client is the growth REST transport. Origin/Referer/User-Agent are mandatory:
// the APISIX gateway in front of workbuddy.cn answers 401 without the browser
// fingerprint.
type Client struct {
	baseURL string
	// origin is scheme://host with no path. The gateway compares Origin
	// literally, so it must never carry the request path.
	origin string
	http   *http.Client
}

// NewClient builds a growth client. A zero timeout falls back to the check-in
// timeout, matching the console's client construction.
func NewClient(baseURL string, timeout time.Duration) *Client {
	if timeout <= 0 {
		timeout = 15 * time.Second
	}
	base := strings.TrimRight(strings.TrimSpace(baseURL), "/")
	if base == "" {
		base = defaultGrowthBase
	}
	return &Client{
		baseURL: base,
		origin:  originOf(base),
		http: &http.Client{
			// Proxy explicitly disabled: this machine runs a TUN proxy and a
			// pooled connection through it stalled for 200 seconds.
			Transport: &http.Transport{Proxy: nil, MaxIdleConnsPerHost: 4},
			Timeout:   timeout,
		},
	}
}

// originOf strips the path from an absolute URL, leaving scheme://host.
func originOf(rawURL string) string {
	parsed, err := url.Parse(rawURL)
	if err != nil || parsed.Scheme == "" || parsed.Host == "" {
		return rawURL
	}
	return parsed.Scheme + "://" + parsed.Host
}

func (c *Client) headers(token string) map[string]string {
	return map[string]string{
		"Authorization": "Bearer " + token,
		"Accept":        "application/json",
		"User-Agent":    growthUserAgent,
		"Origin":        c.origin,
		"Referer":       c.origin + "/profile/growth-center",
	}
}

const growthUserAgent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

// Overview is the read-only growth snapshot the console renders.
type Overview struct {
	Profile map[string]any `json:"profile"`
	Tasks   []Task         `json:"tasks"`
	Heatmap Heatmap        `json:"heatmap"`
	Streak  Streak         `json:"streak"`
	Lottery Lottery        `json:"lottery"`
}

// Task is one growth-centre task.
type Task struct {
	TaskCode      string
	Title         string
	TaskDesc      string
	TaskType      string
	Tag           string
	AcceptStatus  string
	ProgressCur   *float64
	ProgressTgt   *float64
	RewardCredit  float64
	RewardEnergy  float64
	HasReward     bool
	RewardClaimed bool
	Claimed       bool
	IsClaimed     bool
	ReceiveStatus string
	Locked        bool
	IsNew         bool
	IconURL       string
}

// HeatmapCell is one day cell of the growth heatmap.
type HeatmapCell struct {
	Date        string
	Score       *float64
	HasNewBuddy bool
}

// HeatmapToday is the upstream "today" summary. It uses the upstream day
// boundary, so it is only trusted when its own date matches the local date.
type HeatmapToday struct {
	Date       string
	Score      *float64
	IsActive   bool
	StatusText string
	UpdatedAt  string
}

// Heatmap is the growth heatmap payload.
type Heatmap struct {
	Cells      []HeatmapCell
	Today      *HeatmapToday
	RangeStart string
	RangeEnd   string
	UpdatedAt  string
}

// Streak is the consecutive-login summary.
type Streak struct {
	Days          *int
	NextTier      string
	NextRemaining *int
	MakeupBalance *int
	MakeupMax     *int
	RemainingDays *int
	Timezone      string
}

// Lottery is the read-only lottery summary.
type Lottery struct {
	AvailableChances *int
	TotalDraws       *int
}

// Fetch performs the five read-only growth GETs. Any failure aborts the whole
// fetch, matching the Python client.
func (c *Client) Fetch(ctx context.Context, token string) (Overview, error) {
	headers := c.headers(token)
	profile, err := c.get(ctx, "/v2/activity/growth/profile", headers)
	if err != nil {
		return Overview{}, err
	}
	tasks, err := c.get(ctx, "/v2/activity/growth/tasks", headers)
	if err != nil {
		return Overview{}, err
	}
	heatmap, err := c.get(ctx, "/activity/growth/heatmap", headers)
	if err != nil {
		return Overview{}, err
	}
	streak, err := c.get(ctx, "/activity/growth/streak", headers)
	if err != nil {
		return Overview{}, err
	}
	lottery, err := c.get(ctx, "/activity/growth/lottery/summary", headers)
	if err != nil {
		return Overview{}, err
	}
	return Overview{
		Profile: normaliseProfile(profile),
		Tasks:   normaliseTasks(tasks),
		Heatmap: normaliseHeatmap(heatmap),
		Streak:  normaliseStreak(streak),
		Lottery: normaliseLottery(lottery),
	}, nil
}

// JSON renders the overview in the exact key shape the console consumes.
func (o Overview) JSON() map[string]any {
	tasks := make([]map[string]any, 0, len(o.Tasks))
	for _, task := range o.Tasks {
		tasks = append(tasks, task.JSON())
	}
	return map[string]any{
		"profile": o.Profile,
		"tasks":   tasks,
		"heatmap": o.Heatmap.JSON(),
		"streak":  o.Streak.JSON(),
		"lottery": o.Lottery.JSON(),
	}
}

// JSON renders one task in the console's key shape.
func (t Task) JSON() map[string]any {
	return map[string]any{
		"task_code":        t.TaskCode,
		"title":            t.Title,
		"task_desc":        t.TaskDesc,
		"task_type":        t.TaskType,
		"tag":              t.Tag,
		"accept_status":    t.AcceptStatus,
		"progress_current": t.ProgressCur,
		"progress_target":  t.ProgressTgt,
		"reward_credit":    t.RewardCredit,
		"reward_energy":    t.RewardEnergy,
		"has_reward":       t.HasReward,
		"reward_claimed":   t.RewardClaimed,
		"claimed":          t.Claimed,
		"is_claimed":       t.IsClaimed,
		"receive_status":   t.ReceiveStatus,
		"locked":           t.Locked,
		"is_new":           t.IsNew,
		"icon_url":         t.IconURL,
	}
}

// JSON renders the heatmap in the console's key shape.
func (h Heatmap) JSON() map[string]any {
	cells := make([]map[string]any, 0, len(h.Cells))
	for _, cell := range h.Cells {
		cells = append(cells, map[string]any{
			"date":          cell.Date,
			"score":         cell.Score,
			"has_new_buddy": cell.HasNewBuddy,
		})
	}
	var today any
	if h.Today != nil {
		today = map[string]any{
			"date":        h.Today.Date,
			"score":       h.Today.Score,
			"is_active":   h.Today.IsActive,
			"status_text": h.Today.StatusText,
		}
	}
	return map[string]any{
		"cells":       cells,
		"today":       today,
		"range_start": h.RangeStart,
		"range_end":   h.RangeEnd,
		"updated_at":  h.UpdatedAt,
	}
}

// JSON renders the streak summary in the console's key shape.
func (s Streak) JSON() map[string]any {
	return map[string]any{
		"days":                s.Days,
		"next_tier":           s.NextTier,
		"next_tier_remaining": s.NextRemaining,
		"makeup_balance":      s.MakeupBalance,
		"makeup_max":          s.MakeupMax,
		"remaining_days":      s.RemainingDays,
		"timezone":            s.Timezone,
	}
}

// JSON renders the lottery summary in the console's key shape.
func (l Lottery) JSON() map[string]any {
	return map[string]any{
		"available_chances": l.AvailableChances,
		"total_draws":       l.TotalDraws,
	}
}

// ---- step mutations ----

// AcceptTasks accepts every pending task code in one call.
func (c *Client) AcceptTasks(ctx context.Context, token string, codes []string) (map[string]any, error) {
	return c.post(ctx, "/activity/growth/tasks/accept", c.headers(token), map[string]any{"task_codes": codes})
}

// ClaimTask claims one task's reward.
func (c *Client) ClaimTask(ctx context.Context, token, code string) (map[string]any, error) {
	return c.post(ctx, "/activity/growth/tasks/"+code+"/claim", c.headers(token), map[string]any{})
}

// LotteryDraw spends one lottery chance. The client token makes the draw
// idempotent upstream, so it is freshly minted per call.
func (c *Client) LotteryDraw(ctx context.Context, token string) (map[string]any, error) {
	return c.post(ctx, "/activity/growth/lottery/draw", c.headers(token),
		map[string]any{"client_token": "draw-" + uuid4()})
}

// TravelStatus reads the current buddy travel state.
func (c *Client) TravelStatus(ctx context.Context, token string) (map[string]any, error) {
	return c.get(ctx, "/activity/growth/buddy/travel/status", c.headers(token))
}

// TravelConfig reads the available travel destinations.
func (c *Client) TravelConfig(ctx context.Context, token string) (map[string]any, error) {
	return c.get(ctx, "/activity/growth/buddy/travel/config", c.headers(token))
}

// TravelDepart sends the buddy to one destination.
func (c *Client) TravelDepart(ctx context.Context, token string, locationID int) (map[string]any, error) {
	return c.post(ctx, "/activity/growth/buddy/travel/depart", c.headers(token),
		map[string]any{"location_id": locationID})
}

// TravelClaim claims the finished trip's reward.
func (c *Client) TravelClaim(ctx context.Context, token string) (map[string]any, error) {
	return c.post(ctx, "/activity/growth/buddy/travel/claim", c.headers(token), map[string]any{})
}

// RedeemSummary reads the consecutive-day redemption status.
func (c *Client) RedeemSummary(ctx context.Context, token string) (map[string]any, error) {
	return c.get(ctx, "/activity/growth/redeem/summary", c.headers(token))
}

// Redeem redeems one tier of the consecutive-day reward.
func (c *Client) Redeem(ctx context.Context, token, tier string) (map[string]any, error) {
	return c.post(ctx, "/activity/growth/redeem", c.headers(token),
		map[string]any{"tier": tier, "client_token": "redeem-" + tier + "-" + uuid4()})
}

// BuddyQuota reads how many buddies the account can still open.
func (c *Client) BuddyQuota(ctx context.Context, token string) (map[string]any, error) {
	return c.get(ctx, "/activity/growth/buddy/quota", c.headers(token))
}

// BuddyOpen spends energy to open buddies.
func (c *Client) BuddyOpen(ctx context.Context, token string, count int) (map[string]any, error) {
	return c.post(ctx, "/activity/growth/buddy/open", c.headers(token), map[string]any{"count": count})
}

// MakeupUse consumes a makeup card for a missed day. No automation step drives
// it; it exists because the console exposes the operation.
func (c *Client) MakeupUse(ctx context.Context, token, targetDate string) (map[string]any, error) {
	return c.post(ctx, "/activity/growth/makeup-cards/use", c.headers(token),
		map[string]any{"target_date": targetDate})
}

func (c *Client) get(ctx context.Context, path string, headers map[string]string) (map[string]any, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+path, nil)
	if err != nil {
		return nil, growthError("transport:" + typeName(err))
	}
	return c.do(request, headers)
}

func (c *Client) post(ctx context.Context, path string, headers map[string]string, body map[string]any) (map[string]any, error) {
	encoded, err := json.Marshal(body)
	if err != nil {
		return nil, growthError("transport:json")
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, strings.NewReader(string(encoded)))
	if err != nil {
		return nil, growthError("transport:" + typeName(err))
	}
	headers["Content-Type"] = "application/json"
	return c.do(request, headers)
}

// do runs one growth request and unwraps the {code, data} envelope.
func (c *Client) do(request *http.Request, headers map[string]string) (map[string]any, error) {
	for key, value := range headers {
		request.Header.Set(key, value)
	}
	response, err := c.http.Do(request)
	if err != nil {
		return nil, growthError("transport:" + typeName(err))
	}
	defer response.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(response.Body, maxResponseBytes))
	if err != nil {
		return nil, growthError("transport:" + typeName(err))
	}
	if response.StatusCode == http.StatusUnauthorized {
		return nil, growthError("auth_rejected")
	}
	if response.StatusCode < 200 || response.StatusCode > 299 {
		return nil, growthError(fmt.Sprintf("http:%d", response.StatusCode))
	}
	var body map[string]any
	if err := json.Unmarshal(raw, &body); err != nil {
		return nil, growthError("upstream_error")
	}
	code, ok := body["code"]
	if !ok || !isZeroCode(code) {
		return nil, growthError("upstream_error")
	}
	if data, ok := body["data"].(map[string]any); ok {
		return data, nil
	}
	return map[string]any{}, nil
}

// maxResponseBytes bounds a growth response. The payloads are small; the bound
// exists so a hostile upstream cannot make the process allocate without limit.
const maxResponseBytes = 4 << 20

func isZeroCode(value any) bool {
	switch typed := value.(type) {
	case float64:
		return typed == 0
	case string:
		return typed == "0"
	}
	return false
}

// ---- normalisation ----

func normaliseProfile(data map[string]any) map[string]any {
	return map[string]any{
		"level":       data["level"],
		"completed":   data["completed"],
		"total":       data["total"],
		"max_level":   data["max_level"],
		"first_visit": data["first_visit"],
	}
}

func normaliseTasks(data map[string]any) []Task {
	raw, ok := data["tasks"].([]any)
	if !ok {
		return []Task{}
	}
	tasks := make([]Task, 0, len(raw))
	for _, item := range raw {
		entry, ok := item.(map[string]any)
		if !ok {
			continue
		}
		progress, _ := entry["progress"].(map[string]any)
		var current, target *float64
		if progress != nil {
			current = numberPtr(progress["current"])
			target = numberPtr(progress["target"])
		}
		tasks = append(tasks, Task{
			TaskCode:      stringField(entry["task_code"]),
			Title:         stringField(entry["title"]),
			TaskDesc:      stringField(entry["task_desc"]),
			TaskType:      stringField(entry["task_type"]),
			Tag:           stringField(entry["tag"]),
			AcceptStatus:  stringField(entry["accept_status"]),
			ProgressCur:   current,
			ProgressTgt:   target,
			RewardCredit:  numberValue(entry["reward_credit"]),
			RewardEnergy:  numberValue(entry["reward_energy"]),
			HasReward:     boolField(entry["has_reward"]),
			RewardClaimed: boolField(entry["reward_claimed"]),
			Claimed:       boolField(entry["claimed"]),
			IsClaimed:     boolField(entry["is_claimed"]),
			ReceiveStatus: stringField(entry["receive_status"]),
			Locked:        boolField(entry["locked"]),
			IsNew:         boolField(entry["is_new"]),
			IconURL:       stringField(entry["icon_url"]),
		})
	}
	return tasks
}

func normaliseHeatmap(data map[string]any) Heatmap {
	heatmap := Heatmap{
		Cells:      []HeatmapCell{},
		RangeStart: nestedString(data["range"], "start_date"),
		RangeEnd:   nestedString(data["range"], "end_date"),
		UpdatedAt:  stringField(data["updated_at"]),
	}
	if raw, ok := data["cells"].([]any); ok {
		for _, item := range raw {
			entry, ok := item.(map[string]any)
			if !ok {
				continue
			}
			heatmap.Cells = append(heatmap.Cells, HeatmapCell{
				Date:        stringField(entry["date"]),
				Score:       numberPtr(entry["score"]),
				HasNewBuddy: boolField(entry["has_new_buddy"]),
			})
		}
	}
	if today, ok := data["today"].(map[string]any); ok {
		heatmap.Today = &HeatmapToday{
			Date:       stringField(today["date"]),
			Score:      numberPtr(today["score"]),
			IsActive:   boolField(today["is_active"]),
			StatusText: stringField(today["status_text"]),
			UpdatedAt:  stringField(today["updated_at"]),
		}
	}
	return heatmap
}

func normaliseStreak(data map[string]any) Streak {
	streak, _ := data["streak"].(map[string]any)
	makeup, _ := data["makeup_cards"].(map[string]any)
	redemption, _ := data["redemption_status"].(map[string]any)
	summary := Streak{Timezone: stringField(data["timezone"])}
	if streak != nil {
		summary.Days = intPtr(streak["days"])
		summary.NextTier = stringField(streak["next_tier"])
		summary.NextRemaining = intPtr(streak["next_tier_remaining"])
	}
	if makeup != nil {
		summary.MakeupBalance = intPtr(makeup["balance"])
		summary.MakeupMax = intPtr(makeup["max"])
	}
	if redemption != nil {
		summary.RemainingDays = intPtr(redemption["remaining_days"])
	}
	return summary
}

func normaliseLottery(data map[string]any) Lottery {
	summary := Lottery{TotalDraws: intPtr(data["total_draws"])}
	if chances := data["available_chances"]; chances != nil {
		summary.AvailableChances = intPtr(chances)
	} else {
		summary.AvailableChances = intPtr(data["chances"])
	}
	return summary
}

// ---- small map helpers ----

func stringField(value any) string {
	text, _ := value.(string)
	return text
}

func nestedString(value any, key string) string {
	container, ok := value.(map[string]any)
	if !ok {
		return ""
	}
	return stringField(container[key])
}

func boolField(value any) bool {
	flag, _ := value.(bool)
	return flag
}

func numberValue(value any) float64 {
	if parsed := numberPtr(value); parsed != nil {
		return *parsed
	}
	return 0
}

func numberPtr(value any) *float64 {
	switch typed := value.(type) {
	case float64:
		return &typed
	case float32:
		converted := float64(typed)
		return &converted
	case int:
		converted := float64(typed)
		return &converted
	case int64:
		converted := float64(typed)
		return &converted
	case json.Number:
		parsed, err := typed.Float64()
		if err != nil {
			return nil
		}
		return &parsed
	}
	return nil
}

func intPtr(value any) *int {
	parsed := numberPtr(value)
	if parsed == nil {
		return nil
	}
	converted := int(*parsed)
	return &converted
}

func typeName(err error) string {
	if err == nil {
		return "Error"
	}
	message := fmt.Sprintf("%T", err)
	if index := strings.LastIndex(message, "."); index >= 0 {
		message = message[index+1:]
	}
	return message
}

// uuid4 renders a random RFC-4122 v4 identifier for the upstream client_token
// fields. The random source is the crypto one; the tokens are opaque to us and
// only need to be unique per call.
func uuid4() string {
	buffer := make([]byte, 16)
	if _, err := rand.Read(buffer); err != nil {
		return hex.EncodeToString(buffer)
	}
	buffer[6] = (buffer[6] & 0x0f) | 0x40
	buffer[8] = (buffer[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", buffer[0:4], buffer[4:6], buffer[6:8], buffer[8:10], buffer[10:16])
}

// defaultGrowthBase is the domestic growth endpoint root.
const defaultGrowthBase = "https://www.workbuddy.cn"

// growthBase resolves the configured growth base, falling back to the default.
func growthBase(settings config.Settings) string {
	if strings.TrimSpace(settings.CodeBuddyCheckinBase) == "" {
		return defaultGrowthBase
	}
	return strings.TrimRight(settings.CodeBuddyCheckinBase, "/")
}

// defaultCodeBuddyEndpoint is the console/ACP root.
const defaultCodeBuddyEndpoint = "https://copilot.tencent.com"

func codeBuddyEndpoint(settings config.Settings) string {
	if strings.TrimSpace(settings.CodeBuddyEndpoint) == "" {
		return defaultCodeBuddyEndpoint
	}
	return strings.TrimRight(settings.CodeBuddyEndpoint, "/")
}
