package store

import (
	"context"
	"database/sql"
	"errors"
	"time"
)

// CheckinRun is one row of checkin_runs.
type CheckinRun struct {
	RunID        string  `json:"run_id"`
	LocalDate    string  `json:"local_date"`
	Timezone     string  `json:"timezone"`
	StartedAt    string  `json:"started_at"`
	FinishedAt   *string `json:"finished_at"`
	Status       string  `json:"status"`
	Trigger      string  `json:"trigger"`
	ErrorMessage *string `json:"error_message"`
}

// CheckinAttempt is one row of checkin_attempts.
type CheckinAttempt struct {
	RunID             string   `json:"run_id"`
	Provider          string   `json:"provider"`
	AccountID         string   `json:"account_id"`
	Outcome           *string  `json:"outcome"`
	HTTPStatus        *int     `json:"http_status"`
	BusinessCode      *string  `json:"business_code"`
	RequestID         *string  `json:"request_id"`
	Attempts          int      `json:"attempts"`
	StartedAt         *string  `json:"started_at"`
	FinishedAt        *string  `json:"finished_at"`
	RedactedError     *string  `json:"redacted_error"`
	RewardCredits     *float64 `json:"reward_credits"`
	RewardExpiresAt   *string  `json:"reward_expires_at"`
	QuotaBeforeJSON   *string  `json:"-"`
	QuotaAfterJSON    *string  `json:"-"`
	QuotaDeltaJSON    *string  `json:"-"`
	QuotaObservedAt   *string  `json:"quota_observed_at"`
	QuotaChangeStatus *string  `json:"quota_change_status"`
}

// CheckinDailyState is one row of checkin_daily_state.
type CheckinDailyState struct {
	Provider        string  `json:"provider"`
	AccountID       string  `json:"account_id"`
	LocalDate       string  `json:"local_date"`
	Timezone        string  `json:"timezone"`
	TerminalOutcome *string `json:"terminal_outcome"`
	LastRunID       *string `json:"last_run_id"`
	UpdatedAt       string  `json:"updated_at"`
}

// CreateCheckinRun opens a new batch run row.
func (d *DB) CreateCheckinRun(ctx context.Context, run CheckinRun) error {
	if run.StartedAt == "" {
		run.StartedAt = NowISO()
	}
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT INTO checkin_runs (run_id, local_date, timezone, started_at, status, trigger)
			VALUES (?, ?, ?, ?, ?, ?)`,
			run.RunID, run.LocalDate, run.Timezone, run.StartedAt,
			orDefault(run.Status, "running"), orDefault(run.Trigger, "scheduler"))
		return err
	})
}

// FinishCheckinRun closes a batch run row.
func (d *DB) FinishCheckinRun(ctx context.Context, runID, status, errorMessage string) error {
	finished := NowISO()
	var message *string
	if errorMessage != "" {
		message = &errorMessage
	}
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx,
			"UPDATE checkin_runs SET finished_at=?, status=?, error_message=? WHERE run_id=?",
			finished, status, message, runID)
		return err
	})
}

// GetCheckinRun loads one batch run.
func (d *DB) GetCheckinRun(ctx context.Context, runID string) (CheckinRun, error) {
	var run CheckinRun
	err := d.QueryRowContext(ctx, `
		SELECT run_id, local_date, timezone, started_at, finished_at, status, trigger, error_message
		  FROM checkin_runs WHERE run_id=?`, runID).
		Scan(&run.RunID, &run.LocalDate, &run.Timezone, &run.StartedAt, &run.FinishedAt,
			&run.Status, &run.Trigger, &run.ErrorMessage)
	if errors.Is(err, sql.ErrNoRows) {
		return CheckinRun{}, ErrNotFound
	}
	return run, err
}

// ListCheckinRuns returns batch runs newest-first.
func (d *DB) ListCheckinRuns(ctx context.Context, status, trigger string, limit, offset int) ([]CheckinRun, error) {
	limit = clamp(limit, 1, 501)
	query := `SELECT run_id, local_date, timezone, started_at, finished_at, status, trigger, error_message
	            FROM checkin_runs`
	clauses := []string{}
	args := []any{}
	if status != "" {
		clauses = append(clauses, "status=?")
		args = append(args, status)
	}
	if trigger != "" {
		clauses = append(clauses, "trigger=?")
		args = append(args, trigger)
	}
	if len(clauses) > 0 {
		query += " WHERE " + joinAnd(clauses)
	}
	query += " ORDER BY started_at DESC, run_id DESC LIMIT ? OFFSET ?"
	args = append(args, limit, offset)
	rows, err := d.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []CheckinRun
	for rows.Next() {
		var run CheckinRun
		if err := rows.Scan(&run.RunID, &run.LocalDate, &run.Timezone, &run.StartedAt, &run.FinishedAt,
			&run.Status, &run.Trigger, &run.ErrorMessage); err != nil {
			return nil, err
		}
		out = append(out, run)
	}
	return out, rows.Err()
}

// CountCheckinRuns counts batch runs matching the filters.
func (d *DB) CountCheckinRuns(ctx context.Context, status, trigger string) (int, error) {
	query := "SELECT COUNT(*) FROM checkin_runs"
	clauses := []string{}
	args := []any{}
	if status != "" {
		clauses = append(clauses, "status=?")
		args = append(args, status)
	}
	if trigger != "" {
		clauses = append(clauses, "trigger=?")
		args = append(args, trigger)
	}
	if len(clauses) > 0 {
		query += " WHERE " + joinAnd(clauses)
	}
	var total int
	err := d.QueryRowContext(ctx, query, args...).Scan(&total)
	return total, err
}

// UpsertCheckinAttempt stores one per-account result.
func (d *DB) UpsertCheckinAttempt(ctx context.Context, attempt CheckinAttempt) error {
	if attempt.StartedAt == nil {
		now := NowISO()
		attempt.StartedAt = &now
	}
	if attempt.FinishedAt == nil {
		now := NowISO()
		attempt.FinishedAt = &now
	}
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT INTO checkin_attempts
				(run_id, provider, account_id, outcome, http_status, business_code, request_id, attempts,
				 started_at, finished_at, redacted_error, reward_credits, reward_expires_at,
				 quota_before_json, quota_after_json, quota_delta_json, quota_observed_at, quota_change_status)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(run_id, provider, account_id) DO UPDATE SET
				outcome=excluded.outcome, http_status=excluded.http_status,
				business_code=excluded.business_code, request_id=excluded.request_id,
				attempts=excluded.attempts, finished_at=excluded.finished_at,
				redacted_error=excluded.redacted_error, reward_credits=excluded.reward_credits,
				reward_expires_at=excluded.reward_expires_at, quota_before_json=excluded.quota_before_json,
				quota_after_json=excluded.quota_after_json, quota_delta_json=excluded.quota_delta_json,
				quota_observed_at=excluded.quota_observed_at, quota_change_status=excluded.quota_change_status`,
			attempt.RunID, attempt.Provider, attempt.AccountID, attempt.Outcome, attempt.HTTPStatus,
			attempt.BusinessCode, attempt.RequestID, attempt.Attempts, attempt.StartedAt, attempt.FinishedAt,
			attempt.RedactedError, attempt.RewardCredits, attempt.RewardExpiresAt, attempt.QuotaBeforeJSON,
			attempt.QuotaAfterJSON, attempt.QuotaDeltaJSON, attempt.QuotaObservedAt, attempt.QuotaChangeStatus)
		return err
	})
}

// ListCheckinAttempts returns the attempts belonging to one run.
func (d *DB) ListCheckinAttempts(ctx context.Context, runID string) ([]CheckinAttempt, error) {
	rows, err := d.QueryContext(ctx, `
		SELECT run_id, provider, account_id, outcome, http_status, business_code, request_id, attempts,
		       started_at, finished_at, redacted_error, reward_credits, reward_expires_at,
		       quota_before_json, quota_after_json, quota_delta_json, quota_observed_at, quota_change_status
		  FROM checkin_attempts WHERE run_id=? ORDER BY provider, account_id`, runID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []CheckinAttempt
	for rows.Next() {
		var attempt CheckinAttempt
		if err := rows.Scan(&attempt.RunID, &attempt.Provider, &attempt.AccountID, &attempt.Outcome,
			&attempt.HTTPStatus, &attempt.BusinessCode, &attempt.RequestID, &attempt.Attempts,
			&attempt.StartedAt, &attempt.FinishedAt, &attempt.RedactedError, &attempt.RewardCredits,
			&attempt.RewardExpiresAt, &attempt.QuotaBeforeJSON, &attempt.QuotaAfterJSON,
			&attempt.QuotaDeltaJSON, &attempt.QuotaObservedAt, &attempt.QuotaChangeStatus); err != nil {
			return nil, err
		}
		out = append(out, attempt)
	}
	return out, rows.Err()
}

// LatestCheckinAttempt returns the most recent attempt for one account.
func (d *DB) LatestCheckinAttempt(ctx context.Context, provider, accountID string) (CheckinAttempt, error) {
	rows, err := d.QueryContext(ctx, `
		SELECT run_id, provider, account_id, outcome, http_status, business_code, request_id, attempts,
		       started_at, finished_at, redacted_error, reward_credits, reward_expires_at,
		       quota_before_json, quota_after_json, quota_delta_json, quota_observed_at, quota_change_status
		  FROM checkin_attempts WHERE provider=? AND account_id=?
		 ORDER BY finished_at DESC LIMIT 1`, provider, accountID)
	if err != nil {
		return CheckinAttempt{}, err
	}
	defer rows.Close()
	var attempt CheckinAttempt
	found := false
	for rows.Next() {
		found = true
		if err := rows.Scan(&attempt.RunID, &attempt.Provider, &attempt.AccountID, &attempt.Outcome,
			&attempt.HTTPStatus, &attempt.BusinessCode, &attempt.RequestID, &attempt.Attempts,
			&attempt.StartedAt, &attempt.FinishedAt, &attempt.RedactedError, &attempt.RewardCredits,
			&attempt.RewardExpiresAt, &attempt.QuotaBeforeJSON, &attempt.QuotaAfterJSON,
			&attempt.QuotaDeltaJSON, &attempt.QuotaObservedAt, &attempt.QuotaChangeStatus); err != nil {
			return CheckinAttempt{}, err
		}
	}
	if !found {
		return CheckinAttempt{}, ErrNotFound
	}
	return attempt, rows.Err()
}

// UpsertCheckinDailyState records the terminal outcome for a day.
func (d *DB) UpsertCheckinDailyState(ctx context.Context, state CheckinDailyState) error {
	if state.UpdatedAt == "" {
		state.UpdatedAt = NowISO()
	}
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT INTO checkin_daily_state
				(provider, account_id, local_date, timezone, terminal_outcome, last_run_id, updated_at)
			VALUES (?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(provider, account_id, local_date, timezone) DO UPDATE SET
				terminal_outcome=excluded.terminal_outcome, last_run_id=excluded.last_run_id,
				updated_at=excluded.updated_at`,
			state.Provider, state.AccountID, state.LocalDate, state.Timezone,
			state.TerminalOutcome, state.LastRunID, state.UpdatedAt)
		return err
	})
}

// GetCheckinDailyState loads the day marker for one account.
func (d *DB) GetCheckinDailyState(ctx context.Context, provider, accountID, localDate, timezone string) (CheckinDailyState, error) {
	var state CheckinDailyState
	err := d.QueryRowContext(ctx, `
		SELECT provider, account_id, local_date, timezone, terminal_outcome, last_run_id, updated_at
		  FROM checkin_daily_state
		 WHERE provider=? AND account_id=? AND local_date=? AND timezone=?`,
		provider, accountID, localDate, timezone).
		Scan(&state.Provider, &state.AccountID, &state.LocalDate, &state.Timezone,
			&state.TerminalOutcome, &state.LastRunID, &state.UpdatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return CheckinDailyState{}, ErrNotFound
	}
	return state, err
}

// ListCheckinDailyStates returns every day marker for one local date.
func (d *DB) ListCheckinDailyStates(ctx context.Context, localDate, timezone string) ([]CheckinDailyState, error) {
	rows, err := d.QueryContext(ctx, `
		SELECT provider, account_id, local_date, timezone, terminal_outcome, last_run_id, updated_at
		  FROM checkin_daily_state WHERE local_date=? AND timezone=?
		 ORDER BY provider, account_id`, localDate, timezone)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []CheckinDailyState
	for rows.Next() {
		var state CheckinDailyState
		if err := rows.Scan(&state.Provider, &state.AccountID, &state.LocalDate, &state.Timezone,
			&state.TerminalOutcome, &state.LastRunID, &state.UpdatedAt); err != nil {
			return nil, err
		}
		out = append(out, state)
	}
	return out, rows.Err()
}

// ActiveDay is one row of workbuddy_active_days.
type ActiveDay struct {
	Provider           string  `json:"provider"`
	AccountID          string  `json:"account_id"`
	LocalDate          string  `json:"local_date"`
	Timezone           string  `json:"timezone"`
	Status             string  `json:"status"`
	ErrorCode          *string `json:"error_code"`
	StartedAt          string  `json:"started_at"`
	FinishedAt         *string `json:"finished_at"`
	UpdatedAt          string  `json:"updated_at"`
	Confirmed          *string `json:"confirmed"`
	ConfirmedAt        *string `json:"confirmed_at"`
	ConfirmAttempts    int     `json:"confirm_attempts"`
	RunAttempts        int     `json:"run_attempts"`
	OfficialScore      *int    `json:"official_score"`
	OfficialStreakDays *int    `json:"official_streak_days"`
	OfficialUpdatedAt  *string `json:"official_updated_at"`
	OfficialObservedAt *string `json:"official_observed_at"`
}

// ClaimActiveDay inserts the day row if absent. It returns true when this call
// created the row, which is the "one ACP conversation per day" lock: the
// insert is the mutual exclusion, so a concurrent runner sees 0 rows affected
// and skips instead of sending a second ACP turn.
func (d *DB) ClaimActiveDay(ctx context.Context, day ActiveDay) (bool, error) {
	if day.StartedAt == "" {
		day.StartedAt = NowISO()
	}
	claimed := false
	err := d.Write(ctx, func(tx *sql.Tx) error {
		result, err := tx.ExecContext(ctx, `
			INSERT INTO workbuddy_active_days
				(provider, account_id, local_date, timezone, status, started_at, updated_at, confirm_attempts, run_attempts)
			VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1)
			ON CONFLICT(provider, account_id, local_date, timezone) DO UPDATE SET
				run_attempts = run_attempts + 1, updated_at = excluded.updated_at`,
			day.Provider, day.AccountID, day.LocalDate, day.Timezone,
			orDefault(day.Status, "running"), day.StartedAt, NowISO())
		if err != nil {
			return err
		}
		affected, err := result.RowsAffected()
		if err != nil {
			return err
		}
		// The upsert always reports one row; distinguish insert from update by
		// reading back whether the run counter advanced past the initial 1.
		if affected != 1 {
			return nil
		}
		var runs int
		if err := tx.QueryRowContext(ctx, `
			SELECT run_attempts FROM workbuddy_active_days
			 WHERE provider=? AND account_id=? AND local_date=? AND timezone=?`,
			day.Provider, day.AccountID, day.LocalDate, day.Timezone).Scan(&runs); err != nil {
			return err
		}
		claimed = runs == 1
		return nil
	})
	return claimed, err
}

// GetActiveDay loads the day row for one account.
func (d *DB) GetActiveDay(ctx context.Context, provider, accountID, localDate, timezone string) (ActiveDay, error) {
	var day ActiveDay
	err := d.QueryRowContext(ctx, `
		SELECT provider, account_id, local_date, timezone, status, error_code, started_at, finished_at,
		       updated_at, confirmed, confirmed_at, confirm_attempts, run_attempts, official_score,
		       official_streak_days, official_updated_at, official_observed_at
		  FROM workbuddy_active_days
		 WHERE provider=? AND account_id=? AND local_date=? AND timezone=?`,
		provider, accountID, localDate, timezone).
		Scan(&day.Provider, &day.AccountID, &day.LocalDate, &day.Timezone, &day.Status, &day.ErrorCode,
			&day.StartedAt, &day.FinishedAt, &day.UpdatedAt, &day.Confirmed, &day.ConfirmedAt,
			&day.ConfirmAttempts, &day.RunAttempts, &day.OfficialScore, &day.OfficialStreakDays,
			&day.OfficialUpdatedAt, &day.OfficialObservedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return ActiveDay{}, ErrNotFound
	}
	return day, err
}

// FinishActiveDay records the outcome of the ACP conversation.
func (d *DB) FinishActiveDay(ctx context.Context, provider, accountID, localDate, timezone, status string, errorCode *string, confirmed string) error {
	finished := NowISO()
	var confirmedValue *string
	if confirmed != "" {
		confirmedValue = &confirmed
	}
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			UPDATE workbuddy_active_days
			   SET status=?, error_code=?, finished_at=?, updated_at=?,
			       confirmed=COALESCE(?, confirmed),
			       confirmed_at=CASE WHEN ? IS NOT NULL THEN ? ELSE confirmed_at END
			 WHERE provider=? AND account_id=? AND local_date=? AND timezone=?`,
			status, errorCode, finished, finished, confirmedValue, confirmedValue, finished,
			provider, accountID, localDate, timezone)
		return err
	})
}

// RecordActiveDayConfirmation stores a post-hoc confirmation attempt.
func (d *DB) RecordActiveDayConfirmation(ctx context.Context, provider, accountID, localDate, timezone, confirmed string, score, streak *int) error {
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			UPDATE workbuddy_active_days
			   SET confirmed=?, confirmed_at=?, confirm_attempts=confirm_attempts+1, updated_at=?,
			       official_score=COALESCE(?, official_score),
			       official_streak_days=COALESCE(?, official_streak_days),
			       official_observed_at=?
			 WHERE provider=? AND account_id=? AND local_date=? AND timezone=?`,
			confirmed, NowISO(), NowISO(), score, streak, NowISO(),
			provider, accountID, localDate, timezone)
		return err
	})
}

// ObserveActiveDay records the official heatmap/streak snapshot for a day row.
//
// It deliberately does NOT touch confirmed/confirm_attempts: the observation
// happens on every run, and bumping the confirmation counter there would
// prematurely flip a day to not_lit before the confirmation window elapsed.
func (d *DB) ObserveActiveDay(ctx context.Context, provider, accountID, localDate, timezone string, score, streak *int, officialUpdatedAt *string) error {
	now := NowISO()
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			UPDATE workbuddy_active_days
			   SET official_score=?, official_streak_days=?, official_updated_at=?,
			       official_observed_at=?, updated_at=?
			 WHERE provider=? AND account_id=? AND local_date=? AND timezone=?`,
			score, streak, officialUpdatedAt, now, now, provider, accountID, localDate, timezone)
		return err
	})
}

// ResetActiveDay rewrites today's row for the manual rerun path, which is
// allowed to bypass the one-per-day lock.
//
// It clears the confirmation state so the next confirm pass re-evaluates the
// day instead of short-circuiting on a stale not_lit verdict. started_at and
// run_attempts are preserved on conflict so the row still records the first
// attempt of the day.
func (d *DB) ResetActiveDay(ctx context.Context, provider, accountID, localDate, timezone, status string, errorCode *string) error {
	now := NowISO()
	return d.Write(ctx, func(tx *sql.Tx) error {
		// finished_at is set in BOTH branches: a rerun on a day that has no prior
		// row must still record a completion time, or the console shows a
		// finished run with a null timestamp.
		_, err := tx.ExecContext(ctx, `
			INSERT INTO workbuddy_active_days
				(provider, account_id, local_date, timezone, status, error_code, started_at, finished_at,
				 updated_at, confirm_attempts, run_attempts)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1)
			ON CONFLICT(provider, account_id, local_date, timezone) DO UPDATE SET
				status=excluded.status, error_code=excluded.error_code,
				finished_at=excluded.finished_at, updated_at=excluded.updated_at,
				confirmed=NULL, confirmed_at=NULL, confirm_attempts=0`,
			provider, accountID, localDate, timezone, orDefault(status, "running"), errorCode,
			now, now, now)
		return err
	})
}

// ListActiveDays returns the active-day rows for one local date.
func (d *DB) ListActiveDays(ctx context.Context, localDate, timezone string) ([]ActiveDay, error) {
	rows, err := d.QueryContext(ctx, `
		SELECT provider, account_id, local_date, timezone, status, error_code, started_at, finished_at,
		       updated_at, confirmed, confirmed_at, confirm_attempts, run_attempts, official_score,
		       official_streak_days, official_updated_at, official_observed_at
		  FROM workbuddy_active_days WHERE local_date=? AND timezone=?
		 ORDER BY provider, account_id`, localDate, timezone)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ActiveDay
	for rows.Next() {
		var day ActiveDay
		if err := rows.Scan(&day.Provider, &day.AccountID, &day.LocalDate, &day.Timezone, &day.Status,
			&day.ErrorCode, &day.StartedAt, &day.FinishedAt, &day.UpdatedAt, &day.Confirmed,
			&day.ConfirmedAt, &day.ConfirmAttempts, &day.RunAttempts, &day.OfficialScore,
			&day.OfficialStreakDays, &day.OfficialUpdatedAt, &day.OfficialObservedAt); err != nil {
			return nil, err
		}
		out = append(out, day)
	}
	return out, rows.Err()
}

// GrowthLogEntry is one row of growth_automation_log.
type GrowthLogEntry struct {
	ID          int64          `json:"id"`
	Provider    string         `json:"provider"`
	AccountID   string         `json:"account_id"`
	TriggeredBy string         `json:"triggered_by"`
	Results     map[string]any `json:"results"`
	CreatedAt   string         `json:"created_at"`
}

// RecordGrowthRun appends one growth automation result.
func (d *DB) RecordGrowthRun(ctx context.Context, provider, accountID, triggeredBy string, results map[string]any) error {
	encoded, err := encodeJSON(results)
	if err != nil {
		return err
	}
	return d.Write(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT INTO growth_automation_log (provider, account_id, triggered_by, results_json, created_at)
			VALUES (?, ?, ?, ?, ?)`,
			provider, accountID, triggeredBy, encoded, NowISO())
		return err
	})
}

// ListGrowthLog returns the automation history for one account, newest-first.
func (d *DB) ListGrowthLog(ctx context.Context, provider, accountID string, limit, offset int) ([]GrowthLogEntry, error) {
	limit = clamp(limit, 1, 501)
	rows, err := d.QueryContext(ctx, `
		SELECT id, provider, account_id, triggered_by, results_json, created_at
		  FROM growth_automation_log WHERE provider=? AND account_id=?
		 ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?`, provider, accountID, limit, offset)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []GrowthLogEntry
	for rows.Next() {
		var entry GrowthLogEntry
		var resultsJSON string
		if err := rows.Scan(&entry.ID, &entry.Provider, &entry.AccountID, &entry.TriggeredBy,
			&resultsJSON, &entry.CreatedAt); err != nil {
			return nil, err
		}
		entry.Results = decodeMap(resultsJSON)
		out = append(out, entry)
	}
	return out, rows.Err()
}

// CountGrowthLog counts history rows for one account.
func (d *DB) CountGrowthLog(ctx context.Context, provider, accountID string) (int, error) {
	var total int
	err := d.QueryRowContext(ctx,
		"SELECT COUNT(*) FROM growth_automation_log WHERE provider=? AND account_id=?",
		provider, accountID).Scan(&total)
	return total, err
}

// LatestGrowthLog returns the most recent automation entry for one account.
func (d *DB) LatestGrowthLog(ctx context.Context, provider, accountID string) (GrowthLogEntry, error) {
	entries, err := d.ListGrowthLog(ctx, provider, accountID, 1, 0)
	if err != nil {
		return GrowthLogEntry{}, err
	}
	if len(entries) == 0 {
		return GrowthLogEntry{}, ErrNotFound
	}
	return entries[0], nil
}

// LocalDate renders the date in a named IANA zone, matching the Python
// `datetime.now(ZoneInfo(tz)).date().isoformat()` helper.
func LocalDate(zone string, at time.Time) string {
	location, err := time.LoadLocation(zone)
	if err != nil {
		location = time.UTC
	}
	return at.In(location).Format("2006-01-02")
}
