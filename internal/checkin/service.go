package checkin

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"log/slog"
	"sort"
	"sync"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/store"
)

// ErrRunInProgress reports that a batch already holds the single run slot.
var ErrRunInProgress = errors.New("checkin_run_in_progress")

// Service runs sign-in batches. One process holds at most one run at a time.
type Service struct {
	options ServiceOptions

	mu     sync.Mutex
	active string
}

// NewService builds the batch service.
func NewService(options ServiceOptions) *Service {
	return &Service{options: options}
}

// IsRunning reports whether a batch currently holds the run slot.
func (s *Service) IsRunning() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.active != ""
}

// ActiveRunID reports the run currently executing, or "".
func (s *Service) ActiveRunID() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.active
}

// claimRun takes the single run slot.
func (s *Service) claimRun(runID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.active != "" {
		return ErrRunInProgress
	}
	s.active = runID
	return nil
}

func (s *Service) releaseRun() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.active = ""
}

// StartBatch claims the run row and executes the batch in the background,
// returning the run id immediately.
//
// The batch must not inherit the caller's request context: an HTTP handler
// returns as soon as it has answered, which would cancel the run mid-flight.
func (s *Service) StartBatch(ctx context.Context, trigger string, targets []AccountRef, skipAlreadyDone bool) (string, error) {
	if s.options.DB == nil {
		return "", errors.New("checkin storage unavailable")
	}
	runID := randomHex(16)
	if err := s.claimRun(runID); err != nil {
		return "", err
	}
	if err := s.options.DB.CreateCheckinRun(ctx, store.CheckinRun{
		RunID:     runID,
		LocalDate: s.localDate(),
		Timezone:  s.options.Settings.CheckinTimezone,
		Status:    "running",
		Trigger:   trigger,
	}); err != nil {
		s.releaseRun()
		return "", err
	}
	go func() {
		// A long deadline rather than none: a wedged upstream should eventually
		// release the slot instead of leaving the console stuck on "running".
		runCtx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
		defer cancel()
		defer s.releaseRun()
		defer func() {
			if recovered := recover(); recovered != nil {
				slog.Error("checkin batch panicked", "run_id", runID, "panic", recovered)
				_ = s.options.DB.FinishCheckinRun(runCtx, runID, "failed", "panic")
			}
		}()
		summary, err := s.execute(runCtx, runID, trigger, targets, skipAlreadyDone)
		status := "finished"
		message := ""
		if err != nil {
			status = "failed"
			message = typeName(err)
		}
		_ = summary
		if err := s.options.DB.FinishCheckinRun(runCtx, runID, status, message); err != nil {
			slog.Warn("failed to finish checkin run", "run_id", runID, "error", err)
		}
	}()
	return runID, nil
}

// RunBatch executes a batch synchronously.
func (s *Service) RunBatch(ctx context.Context, trigger string, targets []AccountRef, skipAlreadyDone bool) (RunSummary, error) {
	runID := randomHex(16)
	if err := s.claimRun(runID); err != nil {
		return RunSummary{}, err
	}
	defer s.releaseRun()
	if err := s.options.DB.CreateCheckinRun(ctx, store.CheckinRun{
		RunID:     runID,
		LocalDate: s.localDate(),
		Timezone:  s.options.Settings.CheckinTimezone,
		Status:    "running",
		Trigger:   trigger,
	}); err != nil {
		return RunSummary{}, err
	}
	summary, err := s.execute(ctx, runID, trigger, targets, skipAlreadyDone)
	status, message := "finished", ""
	if err != nil {
		status, message = "failed", typeName(err)
	}
	if finishErr := s.options.DB.FinishCheckinRun(ctx, runID, status, message); finishErr != nil {
		slog.Warn("failed to finish checkin run", "run_id", runID, "error", finishErr)
	}
	return summary, err
}

// execute walks the resolved targets and persists one attempt per account.
func (s *Service) execute(ctx context.Context, runID, trigger string, targets []AccountRef, skipAlreadyDone bool) (RunSummary, error) {
	resolved := s.resolveTargets(trigger, targets)
	// Deterministic order: the domestic pool first, then by account id. Without
	// this, the order follows map iteration and two runs of the same batch
	// produce different attempt orders, which makes comparing them useless.
	sort.SliceStable(resolved, func(i, j int) bool {
		left, right := resolved[i], resolved[j]
		if (left.Provider != "codebuddy") != (right.Provider != "codebuddy") {
			return left.Provider == "codebuddy"
		}
		return left.AccountID < right.AccountID
	})

	summary := RunSummary{RunID: runID, Status: "finished"}
	for _, target := range resolved {
		if ctx.Err() != nil {
			summary.Status = "cancelled"
			return summary, ctx.Err()
		}
		result := s.runOne(ctx, runID, target, skipAlreadyDone)
		summary.Results = append(summary.Results, result)
		if err := s.persistAttempt(ctx, runID, target, result); err != nil {
			slog.Warn("failed to persist checkin attempt",
				"run_id", runID, "account", target.AccountID, "error", err)
		}
	}
	return summary, nil
}

// resolveTargets expands an empty target list into the eligible set. The verify
// trigger bypasses eligibility because it exists to check a specific account.
func (s *Service) resolveTargets(trigger string, targets []AccountRef) []AccountRef {
	if trigger == "verify" {
		return append([]AccountRef(nil), targets...)
	}
	if len(targets) > 0 {
		return append([]AccountRef(nil), targets...)
	}
	if s.options.Registry == nil {
		return nil
	}
	return s.options.Registry.EligibleForPurpose("codebuddy", "checkin")
}

// runOne performs the sign-in for one account, including retries.
func (s *Service) runOne(ctx context.Context, runID string, target AccountRef, skipAlreadyDone bool) Result {
	if skipAlreadyDone && s.alreadyTerminal(ctx, target) {
		return Result{
			Outcome:   OutcomeSkipped,
			Provider:  target.Provider,
			AccountID: target.AccountID,
			Message:   "already terminal today",
		}
	}
	credential, err := s.credentialFor(ctx, target)
	if err != nil {
		return Result{
			Outcome:   OutcomeSkipped,
			Provider:  target.Provider,
			AccountID: target.AccountID,
			Message:   "no checkin credential",
		}
	}
	before := s.quotaSnapshot(ctx, target)

	result, attempts := s.runWithRetry(ctx, target, credential)
	slog.Debug("checkin attempt", "account", target.AccountID, "outcome", result.Outcome, "attempts", attempts)

	if result.OK() {
		s.observeQuota(ctx, target, &result, before)
	}
	s.updatePurpose(ctx, target, result)
	return result
}

// runWithRetry retries only the outcomes the upstream can plausibly recover from
// on its own; retrying a rejected credential or a bad request just burns quota.
func (s *Service) runWithRetry(ctx context.Context, target AccountRef, credential Credential) (Result, int) {
	limit := 0
	if s.options.Runtime != nil {
		limit = s.options.Runtime.CheckinRetryLimit()
	}
	attempts := 0
	for {
		attempts++
		result, err := s.options.Client.Run(ctx, target.AccountID, credential)
		if err != nil {
			result = Result{
				Outcome:   OutcomeFailed,
				Provider:  target.Provider,
				AccountID: target.AccountID,
				Message:   "isolated failure: " + typeName(err),
			}
		}
		if attempts > limit || !shouldRetry(result) {
			return result, attempts
		}
		delay := retryJitter(attempts)
		select {
		case <-ctx.Done():
			return result, attempts
		case <-time.After(delay):
		}
	}
}

// shouldRetry decides whether an outcome is worth another call.
func shouldRetry(result Result) bool {
	status := 0
	if result.HTTPStatus != nil {
		status = *result.HTTPStatus
	}
	switch result.Outcome {
	case OutcomeRateLimited:
		return status == 0 || status == 429
	case OutcomeTransientError:
		return status == 0 || status == 429 || status == 502 || status == 503 || status == 504
	}
	return false
}

// retryJitter backs off exponentially with jitter, capped at 8 seconds.
func retryJitter(attempt int) time.Duration {
	ceiling := 500 * (1 << (attempt - 1))
	if ceiling > 8000 {
		ceiling = 8000
	}
	if ceiling <= 0 {
		return 0
	}
	return time.Duration(randomInt(ceiling+1)) * time.Millisecond
}

// alreadyTerminal reports whether today's day marker already records a success.
func (s *Service) alreadyTerminal(ctx context.Context, target AccountRef) bool {
	state, err := s.options.DB.GetCheckinDailyState(ctx, target.Provider, target.AccountID,
		s.localDate(), s.options.Settings.CheckinTimezone)
	if err != nil || state.TerminalOutcome == nil {
		return false
	}
	outcome := *state.TerminalOutcome
	return outcome == OutcomeClaimed || outcome == OutcomeAlreadyCheckedIn
}

// RunCredential performs one sign-in with an explicitly supplied credential.
//
// It backs import-time verification, where the credential being tested has not
// been stored yet, so it cannot be resolved from the database. The attempt is
// not recorded: it is a probe, not a scheduled run.
func (s *Service) RunCredential(ctx context.Context, accountID string, credential Credential) (Result, error) {
	return s.options.Client.Run(ctx, accountID, credential)
}

// credentialFor resolves the sign-in credential, falling back to the chat
// credential material because a sign-in session is often the same bearer.
func (s *Service) credentialFor(ctx context.Context, target AccountRef) (Credential, error) {
	for _, purpose := range []string{"checkin", "chat"} {
		record, err := s.options.DB.GetCredential(ctx, target.Provider, target.AccountID, purpose)
		if err != nil {
			continue
		}
		payload, err := s.options.Vault.Decrypt(record.EncryptedPayload)
		if err != nil {
			continue
		}
		accessToken, _ := payload["access_token"].(string)
		if accessToken == "" {
			accessToken, _ = payload["token"].(string)
		}
		cookie, _ := payload["cookie"].(string)
		if accessToken == "" && cookie == "" {
			continue
		}
		mode := record.Mode
		if mode == "inherit_chat" || mode == "" {
			if cookie != "" && accessToken != "" {
				mode = "bearer_cookie"
			} else if cookie != "" {
				mode = "cookie"
			} else {
				mode = "bearer"
			}
		}
		return Credential{Mode: mode, AccessToken: accessToken, Cookie: cookie}, nil
	}
	return Credential{}, errors.New("no usable credential")
}

// persistAttempt writes the per-account attempt and, on success, the day marker.
func (s *Service) persistAttempt(ctx context.Context, runID string, target AccountRef, result Result) error {
	attempt := store.CheckinAttempt{
		RunID:             runID,
		Provider:          target.Provider,
		AccountID:         target.AccountID,
		Outcome:           &result.Outcome,
		HTTPStatus:        result.HTTPStatus,
		Attempts:          1,
		RequestID:         result.RequestID,
		RewardCredits:     result.RewardCredits,
		RewardExpiresAt:   result.RewardExpiresAt,
		QuotaObservedAt:   result.QuotaObservedAt,
		QuotaChangeStatus: result.QuotaChangeStatus,
	}
	if result.BusinessCode != nil {
		attempt.BusinessCode = result.BusinessCode
	}
	if !result.OK() {
		message := orDefault(result.Message, result.Outcome)
		attempt.RedactedError = &message
	}
	if json := encodeJSON(result.QuotaBefore); json != "" {
		attempt.QuotaBeforeJSON = &json
	}
	if json := encodeJSON(result.QuotaAfter); json != "" {
		attempt.QuotaAfterJSON = &json
	}
	if json := encodeJSON(result.QuotaDelta); json != "" {
		attempt.QuotaDeltaJSON = &json
	}
	if err := s.options.DB.UpsertCheckinAttempt(ctx, attempt); err != nil {
		return err
	}
	if !result.OK() {
		return nil
	}
	outcome := result.Outcome
	run := runID
	return s.options.DB.UpsertCheckinDailyState(ctx, store.CheckinDailyState{
		Provider:        target.Provider,
		AccountID:       target.AccountID,
		LocalDate:       s.localDate(),
		Timezone:        s.options.Settings.CheckinTimezone,
		TerminalOutcome: &outcome,
		LastRunID:       &run,
	})
}

// updatePurpose mirrors the outcome onto the check-in purpose row.
//
// Only success and an authentication failure change the row: a transient error
// must not silently disable an account that will work on the next run.
func (s *Service) updatePurpose(ctx context.Context, target AccountRef, result Result) {
	purposes, err := s.options.DB.ListPurposes(ctx, target.Provider, target.AccountID)
	if err != nil {
		return
	}
	var current *store.Purpose
	for index := range purposes {
		if purposes[index].Purpose == "checkin" {
			current = &purposes[index]
			break
		}
	}
	if current == nil {
		return
	}
	// Preserve every field the refresh does not own, so a rotation's mirrored
	// expiry is not clobbered by a sign-in run.
	updated := *current
	now := store.NowISO()
	switch {
	case result.OK():
		// A successful sign-in proves the credential, which is exactly the state
		// that should activate the purpose. The Python build wrote enabled=True on
		// success for the same reason: without it an account whose credential
		// works still reads as disabled and never joins the scheduled batch.
		updated.Enabled = true
		updated.Status = "active"
		updated.VerificationStatus = "verified"
		updated.VerifiedAt = &now
		updated.LastSuccessAt = &now
		updated.LastError = nil
	case result.Outcome == OutcomeNeedsReauth:
		updated.Status = "needs_reauth"
		updated.VerificationStatus = "rejected"
		reason := "auth_failed"
		updated.LastError = &reason
	default:
		return
	}
	updated.UpdatedAt = now
	if err := s.options.DB.UpsertPurpose(ctx, updated); err != nil {
		slog.Warn("failed to update checkin purpose", "account", target.AccountID, "error", err)
		return
	}
	if s.options.Registry != nil {
		_ = s.options.Registry.Rebuild(ctx)
	}
}

// Status renders the check-in status view the console reads.
func (s *Service) Status(ctx context.Context, nextRunAt string) (map[string]any, error) {
	timezone := s.options.Settings.CheckinTimezone
	localDate := s.localDate()
	eligible := []map[string]any{}
	if s.options.Registry != nil {
		for _, target := range s.options.Registry.EligibleForPurpose("codebuddy", "checkin") {
			eligible = append(eligible, map[string]any{
				"provider":            target.Provider,
				"account_id":          target.AccountID,
				"status":              target.Status,
				"verification_status": target.VerificationStatus,
				"last_error":          target.LastError,
			})
		}
	}
	states := []map[string]any{}
	stored, err := s.options.DB.ListCheckinDailyStates(ctx, localDate, timezone)
	if err != nil {
		return nil, err
	}
	for _, state := range stored {
		states = append(states, map[string]any{
			"provider":         state.Provider,
			"account_id":       state.AccountID,
			"terminal_outcome": state.TerminalOutcome,
			"last_run_id":      state.LastRunID,
			"updated_at":       state.UpdatedAt,
		})
	}
	response := map[string]any{
		"enabled":           s.enabled(),
		"running":           s.IsRunning(),
		"local_date":        localDate,
		"timezone":          timezone,
		"checkin_at":        s.checkinAt(),
		"next_run_at":       nextRunAt,
		"active_run_id":     nilable(s.ActiveRunID()),
		"eligible_accounts": eligible,
		"daily_states":      states,
	}
	// The last batch is rendered inline so the console can show results without
	// a second round trip.
	runs, err := s.options.DB.ListCheckinRuns(ctx, "", "", 1, 0)
	if err == nil && len(runs) > 0 {
		last := runs[0]
		attempts, _ := s.options.DB.ListCheckinAttempts(ctx, last.RunID)
		results := make([]map[string]any, 0, len(attempts))
		for _, attempt := range attempts {
			results = append(results, map[string]any{
				"provider":            attempt.Provider,
				"account_id":          attempt.AccountID,
				"outcome":             attempt.Outcome,
				"http_status":         attempt.HTTPStatus,
				"business_code":       attempt.BusinessCode,
				"request_id":          attempt.RequestID,
				"message":             attempt.RedactedError,
				"reward_credits":      attempt.RewardCredits,
				"reward_expires_at":   attempt.RewardExpiresAt,
				"quota_observed_at":   attempt.QuotaObservedAt,
				"quota_change_status": attempt.QuotaChangeStatus,
			})
		}
		response["last_run"] = map[string]any{
			"run_id":     last.RunID,
			"status":     last.Status,
			"local_date": last.LocalDate,
			"trigger":    last.Trigger,
			"warnings":   nil,
			"results":    results,
		}
	}
	return response, nil
}

// RunView renders one batch run with its aggregate counters, which are what the
// console's history table displays.
func (s *Service) RunView(ctx context.Context, run store.CheckinRun) map[string]any {
	view := map[string]any{
		"run_id":           run.RunID,
		"started_at":       run.StartedAt,
		"finished_at":      run.FinishedAt,
		"status":           run.Status,
		"trigger":          run.Trigger,
		"attempt_count":    0,
		"successful_count": 0,
	}
	attempts, err := s.options.DB.ListCheckinAttempts(ctx, run.RunID)
	if err != nil {
		return view
	}
	successes := 0
	for _, attempt := range attempts {
		if attempt.Outcome != nil && (*attempt.Outcome == OutcomeClaimed || *attempt.Outcome == OutcomeAlreadyCheckedIn) {
			successes++
		}
	}
	view["attempt_count"] = len(attempts)
	view["successful_count"] = successes
	return view
}

// AttemptView renders one attempt row for the console.
func (s *Service) AttemptView(attempt store.CheckinAttempt) map[string]any {
	view := map[string]any{
		"provider":            attempt.Provider,
		"account_id":          attempt.AccountID,
		"outcome":             attempt.Outcome,
		"http_status":         attempt.HTTPStatus,
		"attempts":            attempt.Attempts,
		"finished_at":         attempt.FinishedAt,
		"error_code":          attempt.RedactedError,
		"reward_credits":      attempt.RewardCredits,
		"reward_expires_at":   attempt.RewardExpiresAt,
		"quota_change_status": attempt.QuotaChangeStatus,
	}
	if attempt.QuotaDeltaJSON != nil {
		var delta map[string]any
		if err := json.Unmarshal([]byte(*attempt.QuotaDeltaJSON), &delta); err == nil {
			view["quota_delta"] = delta
		}
	}
	return view
}

func (s *Service) enabled() bool {
	if s.options.Runtime != nil {
		return s.options.Runtime.CheckinEnabled()
	}
	return s.options.Settings.CheckinEnabled
}

func (s *Service) checkinAt() string {
	if s.options.Runtime != nil {
		return s.options.Runtime.CheckinAt()
	}
	return s.options.Settings.CheckinAt
}

func (s *Service) timezone() string {
	if s.options.Runtime != nil {
		return s.options.Runtime.CheckinTimezone()
	}
	return s.options.Settings.CheckinTimezone
}

func (s *Service) localDate() string {
	return store.LocalDate(s.timezone(), time.Now())
}

// encodeJSON renders a value as compact JSON, returning "" for nil/empty so the
// column stays NULL rather than holding "null".
func encodeJSON(value any) string {
	if value == nil {
		return ""
	}
	raw, err := json.Marshal(value)
	if err != nil || string(raw) == "null" || string(raw) == "{}" {
		return ""
	}
	return string(raw)
}

func nilable(value string) any {
	if value == "" {
		return nil
	}
	return value
}

func randomHex(n int) string {
	raw := make([]byte, (n+1)/2)
	if _, err := rand.Read(raw); err != nil {
		return base64.RawURLEncoding.EncodeToString([]byte(time.Now().Format(time.RFC3339Nano)))[:n]
	}
	const digits = "0123456789abcdef"
	out := make([]byte, n)
	for index := 0; index < n; index++ {
		out[index] = digits[int(raw[index/2])>>4]
		if index%2 == 1 {
			out[index] = digits[int(raw[index/2])&0x0f]
		}
	}
	return string(out)
}

func randomInt(ceiling int) int {
	if ceiling <= 0 {
		return 0
	}
	raw := make([]byte, 4)
	if _, err := rand.Read(raw); err != nil {
		return ceiling / 2
	}
	value := int(raw[0])<<24 | int(raw[1])<<16 | int(raw[2])<<8 | int(raw[3])
	if value < 0 {
		value = -value
	}
	return value % ceiling
}
