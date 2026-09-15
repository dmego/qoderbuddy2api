package growth

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/dmego/qoderbuddy2api/gobackend/internal/config"
	"github.com/dmego/qoderbuddy2api/gobackend/internal/store"
	"github.com/dmego/qoderbuddy2api/gobackend/internal/vault"
)

// Provider is the only provider the growth centre exists for.
const Provider = "codebuddy"

// acpPrompts rotate across runs so a retried day does not replay the same
// prompt; the attempt number selects the entry.
var acpPrompts = []string{"你好", "你是什么模型", "今天几号", "hello", "帮我讲个笑话"}

// redeemDays maps a redeem tier to the consecutive days it requires.
var redeemDays = map[string]int{"7d": 7, "14d": 14, "28d": 28}

// redeemTierStatusKey maps a tier to the summary field that reports whether the
// tier is unlocked.
var redeemTierStatusKey = map[string]string{
	"7d": "starter_status", "14d": "advanced_status", "28d": "legendary_status",
}

// AccountRef identifies one account eligible for a purpose.
type AccountRef struct {
	Provider           string
	AccountID          string
	Status             string
	VerificationStatus string
	LastError          *string
}

// AccountLister yields the accounts eligible for a purpose. It is declared here
// rather than imported so this package stays independent of the check-in
// package's identical interface.
type AccountLister interface {
	EligibleForPurpose(provider, purpose string) []AccountRef
	Rebuild(ctx context.Context) error
}

// MetricsRefresher re-reads one account's credit snapshot after a run that
// earned credits.
type MetricsRefresher interface {
	RefreshOne(ctx context.Context, provider, accountID string) (map[string]any, error)
}

// RuntimeSettings supplies the live overrides written through the settings API.
// Every method must be tolerated when the implementation is absent, so callers
// fall back to the env-derived Settings.
type RuntimeSettings interface {
	GrowthSchedulerEnabled() bool
	GrowthSchedulerIntervalSeconds() int
	GrowthAutoTasks() bool
	GrowthAutoLottery() bool
	GrowthAutoTravel() bool
	GrowthAutoRedeem() bool
	GrowthRedeemTier() string
	GrowthAutoBuddyOpen() bool
	GrowthAutoActiveDay() bool
	GrowthActiveDayConfirmAttempts() int
}

// AutomationError carries a safe code plus the HTTP status the admin routes
// should answer with. The codes are the ones the console already matches on.
type AutomationError struct {
	Code   string
	Detail string
	Status int
}

func (e *AutomationError) Error() string { return e.Code }

func automationError(status int, code string) error {
	return &AutomationError{Code: code, Status: status}
}

// AutomationOptions configures the growth orchestrator.
type AutomationOptions struct {
	DB              *store.DB
	Vault           *vault.Vault
	Settings        config.Settings
	Accounts        AccountLister
	GrowthClient    *Client
	ActiveDayClient *ActiveDayClient
	Metrics         MetricsRefresher
	Runtime         RuntimeSettings
}

// Automation runs the growth-centre steps for one account at a time.
type Automation struct {
	opts     AutomationOptions
	settings config.Settings
	timeout  time.Duration
}

// NewAutomation builds the orchestrator, defaulting the upstream clients from
// settings when they are not injected.
func NewAutomation(opts AutomationOptions) *Automation {
	settings := opts.Settings
	timeout := time.Duration(settings.CheckinTimeout) * time.Second
	if timeout <= 0 {
		timeout = 15 * time.Second
	}
	if opts.GrowthClient == nil {
		opts.GrowthClient = NewClient(growthBase(settings), timeout)
	}
	if opts.ActiveDayClient == nil {
		// The ACP turn outlives the check-in timeout: the upstream streams a
		// whole model answer over SSE.
		acpTimeout := timeout
		if acpTimeout < 60*time.Second {
			acpTimeout = 60 * time.Second
		}
		opts.ActiveDayClient = NewActiveDayClient(codeBuddyEndpoint(settings), acpTimeout)
	}
	return &Automation{opts: opts, settings: settings, timeout: timeout}
}

// ---- settings accessors (runtime override wins, env default otherwise) ----

func (a *Automation) autoTasks() bool {
	if a.opts.Runtime != nil {
		return a.opts.Runtime.GrowthAutoTasks()
	}
	return a.settings.GrowthAutoTasks
}

func (a *Automation) autoLottery() bool {
	if a.opts.Runtime != nil {
		return a.opts.Runtime.GrowthAutoLottery()
	}
	return a.settings.GrowthAutoLottery
}

func (a *Automation) autoTravel() bool {
	if a.opts.Runtime != nil {
		return a.opts.Runtime.GrowthAutoTravel()
	}
	return a.settings.GrowthAutoTravel
}

func (a *Automation) autoRedeem() bool {
	if a.opts.Runtime != nil {
		return a.opts.Runtime.GrowthAutoRedeem()
	}
	return a.settings.GrowthAutoRedeem
}

func (a *Automation) autoBuddyOpen() bool {
	if a.opts.Runtime != nil {
		return a.opts.Runtime.GrowthAutoBuddyOpen()
	}
	return a.settings.GrowthAutoBuddyOpen
}

func (a *Automation) autoActiveDay() bool {
	if a.opts.Runtime != nil {
		return a.opts.Runtime.GrowthAutoActiveDay()
	}
	return a.settings.GrowthAutoActiveDay
}

func (a *Automation) redeemTier() string {
	tier := a.settings.GrowthRedeemTier
	if a.opts.Runtime != nil {
		tier = a.opts.Runtime.GrowthRedeemTier()
	}
	if strings.TrimSpace(tier) == "" {
		return "14d"
	}
	return tier
}

// activeDayAttempts is the confirmation attempt budget, never below one so the
// first unresolved confirmation can still conclude the day.
func (a *Automation) activeDayAttempts() int {
	attempts := a.settings.GrowthActiveDayAttempts
	if a.opts.Runtime != nil {
		attempts = a.opts.Runtime.GrowthActiveDayConfirmAttempts()
	}
	if attempts < 1 {
		return 1
	}
	return attempts
}

// timezone is the zone every day boundary is computed in.
func (a *Automation) timezone() string {
	if strings.TrimSpace(a.settings.CheckinTimezone) == "" {
		return "Asia/Shanghai"
	}
	return a.settings.CheckinTimezone
}

// localDate is today in the configured check-in timezone.
func (a *Automation) localDate() string {
	return store.LocalDate(a.timezone(), time.Now())
}

// ---- credential resolution ----

// resolveToken returns the first non-blank bearer token across the check-in and
// chat purposes, mirroring the console's resolver order.
func (a *Automation) resolveToken(ctx context.Context, provider, accountID string) (string, error) {
	for _, purpose := range []string{"checkin", "chat"} {
		record, err := a.opts.DB.GetCredential(ctx, provider, accountID, purpose)
		if errors.Is(err, store.ErrNotFound) {
			continue
		}
		if err != nil {
			return "", err
		}
		payload, err := a.opts.Vault.Decrypt(record.EncryptedPayload)
		if err != nil {
			continue
		}
		for _, key := range []string{"access_token", "token"} {
			if token, ok := payload[key].(string); ok && strings.TrimSpace(token) != "" {
				return token, nil
			}
		}
	}
	return "", automationError(400, "access_token_missing")
}

// validateAccount reproduces the console's shared growth-account validation for
// the mutation routes: the provider must be the domestic one, an
// environment-managed account is read-only, and the account must be known.
func (a *Automation) validateAccount(ctx context.Context, provider, accountID string) (store.Account, error) {
	if provider != Provider {
		return store.Account{}, automationError(400, "unsupported_provider")
	}
	account, err := a.opts.DB.GetAccount(ctx, provider, accountID)
	if errors.Is(err, store.ErrNotFound) {
		return store.Account{}, automationError(404, "account_not_found")
	}
	if err != nil {
		return store.Account{}, err
	}
	if account.Source == "env" {
		return store.Account{}, automationError(400, "env_account_read_only")
	}
	return account, nil
}

// resolveAccountToken validates the account and resolves its bearer token.
func (a *Automation) resolveAccountToken(ctx context.Context, provider, accountID string) (string, error) {
	if _, err := a.validateAccount(ctx, provider, accountID); err != nil {
		return "", err
	}
	return a.resolveToken(ctx, provider, accountID)
}

// resolveReadToken resolves the token for the read-only overview, which the
// console resolves without the shared validation: an unknown account is a 404
// and only then is a foreign provider rejected.
func (a *Automation) resolveReadToken(ctx context.Context, provider, accountID string) (string, error) {
	if _, err := a.opts.DB.GetAccount(ctx, provider, accountID); errors.Is(err, store.ErrNotFound) {
		return "", automationError(404, "account_not_found")
	} else if err != nil {
		return "", err
	}
	if provider != Provider {
		return "", automationError(400, "unsupported_provider")
	}
	return a.resolveToken(ctx, provider, accountID)
}

// ---- orchestration ----

// RunSteps executes every enabled step and returns the full result map.
func (a *Automation) RunSteps(ctx context.Context, token, accountID, localDate, timezone string) map[string]StepResult {
	results := make(map[string]StepResult, len(StepKeys))
	for _, key := range StepKeys {
		results[key] = skippedResult()
	}
	overview, err := a.opts.GrowthClient.Fetch(ctx, token)
	if err != nil {
		// A failed read must not stop the run: the console renders the reason
		// on every step.
		failure := StepResult{Status: "skipped", Detail: skippedResult().Detail, Error: "fetch_failed:" + unavailableCode(err)}
		for _, key := range StepKeys {
			results[key] = failure
		}
		return results
	}
	if a.autoTasks() {
		results[StepTasks] = a.guard(func() StepResult { return a.stepTasks(ctx, token, overview) })
	}
	if a.autoLottery() {
		results[StepLottery] = a.guard(func() StepResult { return a.stepLottery(ctx, token, overview) })
	}
	if a.autoTravel() {
		results[StepTravel] = a.guard(func() StepResult { return a.stepTravel(ctx, token) })
	}
	if a.autoRedeem() {
		results[StepRedeem] = a.guard(func() StepResult { return a.stepRedeem(ctx, token) })
	}
	if a.autoBuddyOpen() {
		results[StepBuddyOpen] = a.guard(func() StepResult { return a.stepBuddyOpen(ctx, token) })
	}
	if a.autoActiveDay() {
		if accountID == "" || localDate == "" || timezone == "" {
			results[StepActiveDay] = StepResult{Status: "skipped", Detail: "active_day_context_missing"}
		} else {
			results[StepActiveDay] = a.guard(func() StepResult {
				return a.activeDay(ctx, token, accountID, localDate, timezone, overview, true, false)
			})
		}
	}
	return results
}

// RunAccount executes the configured steps for one account, persisting the run
// and refreshing metrics when any step earned credits.
func (a *Automation) RunAccount(ctx context.Context, provider, accountID, triggeredBy string) (map[string]StepResult, error) {
	token, err := a.resolveAccountToken(ctx, provider, accountID)
	if err != nil {
		return nil, err
	}
	localDate := a.localDate()
	timezone := a.timezone()
	results := a.RunSteps(ctx, token, accountID, localDate, timezone)
	if err := a.RecordRun(ctx, provider, accountID, triggeredBy, results); err != nil {
		return results, err
	}
	a.MaybeRefreshMetrics(ctx, provider, accountID, results)
	return results, nil
}

// RunStep executes one named step. An unknown step is a normal, non-error
// outcome: the console expects a failed step result, not an HTTP error.
func (a *Automation) RunStep(ctx context.Context, provider, accountID, step string) (StepResult, error) {
	if !IsStep(step) {
		return StepResult{Status: "failed", Detail: "unknown_step:" + step}, nil
	}
	token, err := a.resolveAccountToken(ctx, provider, accountID)
	if err != nil {
		return StepResult{}, err
	}
	if step == StepActiveDay {
		return a.activeDay(ctx, token, accountID, a.localDate(), a.timezone(), Overview{}, false, false), nil
	}
	overview, fetchErr := a.opts.GrowthClient.Fetch(ctx, token)
	if fetchErr != nil {
		return StepResult{Status: "failed", Detail: "fetch_failed:" + unavailableCode(fetchErr)}, nil
	}
	switch step {
	case StepTasks:
		return a.guard(func() StepResult { return a.stepTasks(ctx, token, overview) }), nil
	case StepLottery:
		return a.guard(func() StepResult { return a.stepLottery(ctx, token, overview) }), nil
	case StepTravel:
		return a.guard(func() StepResult { return a.stepTravel(ctx, token) }), nil
	case StepRedeem:
		return a.guard(func() StepResult { return a.stepRedeem(ctx, token) }), nil
	case StepBuddyOpen:
		return a.guard(func() StepResult { return a.stepBuddyOpen(ctx, token) }), nil
	}
	return StepResult{Status: "failed", Detail: "unknown_step:" + step}, nil
}

// RunActiveDay performs the ACP conversation that lights the day, honouring the
// one-per-day lock in workbuddy_active_days. force bypasses the lock and is the
// operator path that spends a real chat turn.
func (a *Automation) RunActiveDay(ctx context.Context, provider, accountID string, force bool) (StepResult, error) {
	token, err := a.resolveAccountToken(ctx, provider, accountID)
	if err != nil {
		return StepResult{}, err
	}
	return a.activeDay(ctx, token, accountID, a.localDate(), a.timezone(), Overview{}, false, force), nil
}

// activeDay is the shared implementation behind the scheduled step and the
// manual rerun. haveOverview reports whether the caller already holds a
// snapshot for this run; without one the step takes its own.
func (a *Automation) activeDay(ctx context.Context, token, accountID, localDate, timezone string, overview Overview, haveOverview, force bool) StepResult {
	if force {
		return a.forceActiveDay(ctx, token, accountID, localDate, timezone)
	}
	day := store.ActiveDay{
		Provider:  Provider,
		AccountID: accountID,
		LocalDate: localDate,
		Timezone:  timezone,
		Status:    "running",
	}
	// The insert is the mutual exclusion: only the runner that created today's
	// row may send a turn, so a second scheduled pass costs nothing.
	claimed, err := a.opts.DB.ClaimActiveDay(ctx, day)
	if err != nil {
		return StepResult{Status: "failed", Detail: "error:" + typeName(err)}
	}
	if !claimed {
		return StepResult{Status: "skipped", Detail: "already claimed today"}
	}
	fetched := overview
	if !haveOverview {
		// A failed read only costs the official bookkeeping, so it must not
		// block the turn.
		fetched = a.safeFetch(ctx, token)
	}
	a.recordObservation(ctx, accountID, localDate, timezone, fetched)
	a.reconcilePreviousDay(ctx, accountID, localDate, timezone, fetched)
	if todayLit(fetched, localDate) {
		// The upstream already credited a day for this local date, so a turn
		// would be a wasted (paid) conversation.
		confirmed := "lit"
		if err := a.opts.DB.FinishActiveDay(ctx, Provider, accountID, localDate, timezone, "skipped_external", nil, confirmed); err != nil {
			return StepResult{Status: "failed", Detail: "error:" + typeName(err)}
		}
		return StepResult{Status: "skipped_external", Detail: "当天已点亮·跳过"}
	}
	attempt := a.runAttempts(ctx, accountID, localDate, timezone)
	prompt := acpPrompts[(attempt-1)%len(acpPrompts)]
	if err := a.opts.ActiveDayClient.Run(ctx, token, prompt); err != nil {
		code := ErrorCode(err)
		_ = a.opts.DB.FinishActiveDay(ctx, Provider, accountID, localDate, timezone, "failed", &code, "")
		return StepResult{Status: "failed", Error: code, Detail: code}
	}
	if err := a.opts.DB.FinishActiveDay(ctx, Provider, accountID, localDate, timezone, "succeeded", nil, ""); err != nil {
		return StepResult{Status: "failed", Detail: "error:" + typeName(err)}
	}
	return StepResult{
		Status: "pending_confirmation",
		Detail: fmt.Sprintf("第 %d 次 hy3 登录对话已完成，等待官方记账", attempt),
	}
}

// forceActiveDay is the manual rerun: no lock check and no pre-flight lit
// check, because the operator explicitly asked to spend another turn.
func (a *Automation) forceActiveDay(ctx context.Context, token, accountID, localDate, timezone string) StepResult {
	if err := a.opts.ActiveDayClient.Run(ctx, token, acpDefaultPrompt); err != nil {
		code := ErrorCode(err)
		_ = a.opts.DB.ResetActiveDay(ctx, Provider, accountID, localDate, timezone, "failed", &code)
		return StepResult{Status: "failed", Error: code, Detail: code}
	}
	if err := a.opts.DB.ResetActiveDay(ctx, Provider, accountID, localDate, timezone, "succeeded", nil); err != nil {
		return StepResult{Status: "failed", Detail: "error:" + typeName(err)}
	}
	return StepResult{Status: "succeeded"}
}

// runAttempts reports how many ACP turns today's row has recorded. A failed
// read yields attempt 1, which is also the first prompt.
func (a *Automation) runAttempts(ctx context.Context, accountID, localDate, timezone string) int {
	row, err := a.opts.DB.GetActiveDay(ctx, Provider, accountID, localDate, timezone)
	if err != nil || row.RunAttempts < 1 {
		return 1
	}
	return row.RunAttempts
}

// ConfirmActiveDay re-reads the official growth overview and records whether
// today actually lit up. A resolved day is never re-checked upstream.
func (a *Automation) ConfirmActiveDay(ctx context.Context, provider, accountID string) (StepResult, error) {
	token, err := a.resolveAccountToken(ctx, provider, accountID)
	if err != nil {
		return StepResult{}, err
	}
	localDate := a.localDate()
	timezone := a.timezone()
	row, err := a.opts.DB.GetActiveDay(ctx, Provider, accountID, localDate, timezone)
	if errors.Is(err, store.ErrNotFound) {
		return StepResult{Status: "skip_irrelevant"}, nil
	}
	if err != nil {
		return StepResult{}, err
	}
	if row.Status != "succeeded" {
		return StepResult{Status: "skip_irrelevant"}, nil
	}
	if row.Confirmed != nil && *row.Confirmed != "" {
		return StepResult{Status: "confirmed", Detail: *row.Confirmed}, nil
	}
	overview := a.safeFetch(ctx, token)
	score, streak := officialPair(overview, localDate)
	if todayLit(overview, localDate) {
		if err := a.opts.DB.RecordActiveDayConfirmation(ctx, Provider, accountID, localDate, timezone, "lit", score, streak); err != nil {
			return StepResult{}, err
		}
		return StepResult{Status: "lit"}, nil
	}
	// The upstream books the day asynchronously, so an unresolved first
	// confirmation stays pending until the attempt budget runs out.
	if row.ConfirmAttempts+1 >= a.activeDayAttempts() {
		if err := a.opts.DB.RecordActiveDayConfirmation(ctx, Provider, accountID, localDate, timezone, "not_lit", score, streak); err != nil {
			return StepResult{}, err
		}
		return StepResult{Status: "not_lit"}, nil
	}
	if err := a.opts.DB.RecordActiveDayConfirmation(ctx, Provider, accountID, localDate, timezone, "", score, streak); err != nil {
		return StepResult{}, err
	}
	return StepResult{Status: "pending"}, nil
}

// Overview renders the growth page payload: the live normalised snapshot plus
// today's local active-day row.
func (a *Automation) Overview(ctx context.Context, provider, accountID string) (map[string]any, error) {
	token, err := a.resolveReadToken(ctx, provider, accountID)
	if err != nil {
		return nil, err
	}
	overview, err := a.opts.GrowthClient.Fetch(ctx, token)
	if err != nil {
		return nil, &AutomationError{
			Code:   "growth_unavailable:" + unavailableCode(err),
			Status: 502,
		}
	}
	payload := overview.JSON()
	row, err := a.opts.DB.GetActiveDay(ctx, Provider, accountID, a.localDate(), a.timezone())
	if errors.Is(err, store.ErrNotFound) {
		payload["active_day_local"] = nil
		return payload, nil
	}
	if err != nil {
		return nil, err
	}
	payload["active_day_local"] = activeDayLocalView(row)
	return payload, nil
}

// activeDayLocalView is the today-row summary the growth page renders.
func activeDayLocalView(row store.ActiveDay) map[string]any {
	return map[string]any{
		"local_date":       row.LocalDate,
		"status":           row.Status,
		"error_code":       row.ErrorCode,
		"confirmed":        row.Confirmed,
		"confirm_attempts": row.ConfirmAttempts,
		"finished_at":      row.FinishedAt,
	}
}

// RecordRun persists one automation run (or one single-step slice of one).
func (a *Automation) RecordRun(ctx context.Context, provider, accountID, triggeredBy string, results map[string]StepResult) error {
	return a.opts.DB.RecordGrowthRun(ctx, provider, accountID, triggeredBy, stepResultsPayload(results))
}

// MaybeRefreshMetrics re-reads the credit snapshot when a run earned credits,
// because those credits are what the credits page displays.
func (a *Automation) MaybeRefreshMetrics(ctx context.Context, provider, accountID string, results map[string]StepResult) {
	if a.opts.Metrics == nil {
		return
	}
	earned := 0
	for _, result := range results {
		earned += result.Credits()
	}
	if earned == 0 {
		return
	}
	// Best effort: a metrics failure must not fail the automation the operator
	// already triggered.
	_, _ = a.opts.Metrics.RefreshOne(ctx, provider, accountID)
}

// guard converts a panicking step into a failed result so one step cannot abort
// the rest of the run.
func (a *Automation) guard(operation func() StepResult) (result StepResult) {
	defer func() {
		if recovered := recover(); recovered != nil {
			result = StepResult{Status: "failed", Detail: fmt.Sprintf("error:%v", recovered)}
		}
	}()
	return operation()
}

// ---- official-observation helpers ----

// safeFetch reads the overview, swallowing every failure because the callers
// treat a missing snapshot as "no official data yet".
func (a *Automation) safeFetch(ctx context.Context, token string) Overview {
	overview, err := a.opts.GrowthClient.Fetch(ctx, token)
	if err != nil {
		return Overview{}
	}
	return overview
}

// todayLit reports whether the upstream credited an active day for localDate.
// Only the heatmap cell for that exact date is authoritative: heatmap.today
// follows the upstream day boundary, so between local 00:00 and 08:00 it can
// report yesterday as lit and cause a paid turn to be skipped.
func todayLit(overview Overview, localDate string) bool {
	for _, cell := range overview.Heatmap.Cells {
		if cell.Date == localDate {
			return cell.Score != nil && *cell.Score > 0
		}
	}
	today := overview.Heatmap.Today
	if today != nil && today.Date == localDate {
		if today.IsActive {
			return true
		}
		return today.Score != nil && *today.Score > 0
	}
	return false
}

// officialPair extracts the local date's score and the official streak, which
// is what the confirmation row records for diagnosis.
func officialPair(overview Overview, localDate string) (score, streak *int) {
	for _, cell := range overview.Heatmap.Cells {
		if cell.Date == localDate {
			if cell.Score != nil {
				converted := int(*cell.Score)
				score = &converted
			}
			break
		}
	}
	if overview.Streak.Days != nil {
		streak = overview.Streak.Days
	}
	return score, streak
}

// recordObservation stores the official snapshot for the day row. It never
// touches the confirmation counters: that would consume the confirmation budget
// before the operator ever asked for a confirmation.
func (a *Automation) recordObservation(ctx context.Context, accountID, localDate, timezone string, overview Overview) {
	if overview.Heatmap.Cells == nil && overview.Streak.Days == nil {
		return
	}
	score, streak := officialPair(overview, localDate)
	updatedAt := overview.Heatmap.UpdatedAt
	if updatedAt == "" && overview.Heatmap.Today != nil {
		updatedAt = overview.Heatmap.Today.UpdatedAt
	}
	var observed *string
	if updatedAt != "" {
		observed = &updatedAt
	}
	_ = a.opts.DB.ObserveActiveDay(ctx, Provider, accountID, localDate, timezone, score, streak, observed)
}

// reconcilePreviousDay retro-fixes yesterday's confirmation from the official
// heatmap. The upstream books a day asynchronously, so yesterday's row may have
// been concluded too early when the next day's run starts.
func (a *Automation) reconcilePreviousDay(ctx context.Context, accountID, currentDate, timezone string, overview Overview) {
	current, err := time.Parse("2006-01-02", currentDate)
	if err != nil {
		return
	}
	yesterday := current.AddDate(0, 0, -1).Format("2006-01-02")
	var score *float64
	for _, cell := range overview.Heatmap.Cells {
		if cell.Date == yesterday {
			score = cell.Score
			break
		}
	}
	if score == nil {
		return
	}
	row, err := a.opts.DB.GetActiveDay(ctx, Provider, accountID, yesterday, timezone)
	if errors.Is(err, store.ErrNotFound) || err != nil {
		return
	}
	if row.Status != "succeeded" {
		return
	}
	official := int(*score)
	truthLit := official > 0
	confirmed := ""
	if row.Confirmed != nil {
		confirmed = *row.Confirmed
	}
	switch {
	case truthLit && confirmed != "lit":
		_ = a.opts.DB.RecordActiveDayConfirmation(ctx, Provider, accountID, yesterday, timezone, "lit", &official, row.OfficialStreakDays)
	case !truthLit && (confirmed == "" || confirmed == "lit"):
		_ = a.opts.DB.RecordActiveDayConfirmation(ctx, Provider, accountID, yesterday, timezone, "not_lit", &official, row.OfficialStreakDays)
	}
}
