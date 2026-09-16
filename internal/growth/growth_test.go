package growth

import (
	"context"
	"errors"
	"path/filepath"
	"strings"
	"testing"

	"github.com/dmego/qoderbuddy2api/internal/config"
	"github.com/dmego/qoderbuddy2api/internal/store"
	"github.com/dmego/qoderbuddy2api/internal/vault"
)

// Fernet key from the Python test fixtures: any 32-byte urlsafe-base64 key
// works, the tests only need a vault that can round-trip a payload.
const testVaultKey = "3gVhWQoPZ8nS0mCk1xR2uJ4yLb7tE9fQaD6wNcH5iMk="

func testVault(t *testing.T) *vault.Vault {
	t.Helper()
	created, err := vault.New(testVaultKey)
	if err != nil {
		t.Fatalf("vault: %v", err)
	}
	return created
}

// newTestAutomation builds an automation backed by a real (temp) SQLite
// database, one stored account and a stubbed ACP turn.
func newTestAutomation(t *testing.T, turn func(ctx context.Context, token, prompt string) error) (*Automation, *store.DB) {
	t.Helper()
	db, err := store.Open(filepath.Join(t.TempDir(), "growth.sqlite3"))
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	ctx := context.Background()
	if _, err := db.UpsertAccount(ctx, store.Account{
		Provider: Provider, AccountID: "cb-test", Label: "test", Source: "manual", Enabled: true,
	}); err != nil {
		t.Fatalf("seed account: %v", err)
	}
	credentials := testVault(t)
	blob, err := credentials.Encrypt(map[string]any{"access_token": "test-token"})
	if err != nil {
		t.Fatalf("encrypt: %v", err)
	}
	if _, err := db.UpsertCredential(ctx, store.CredentialWrite{
		Provider: Provider, AccountID: "cb-test", Purpose: "checkin",
		Mode: "bearer", EncryptedPayload: blob,
	}); err != nil {
		t.Fatalf("seed credential: %v", err)
	}
	activeDay := NewActiveDayClient("https://copilot.tencent.com", 0)
	if turn != nil {
		activeDay.SetTurnForTest(turn)
	}
	settings := config.Settings{CheckinTimezone: "Asia/Shanghai", GrowthActiveDayAttempts: 3}
	automation := NewAutomation(AutomationOptions{
		DB:              db,
		Vault:           credentials,
		Settings:        settings,
		GrowthClient:    NewClient("https://www.workbuddy.cn", 0),
		ActiveDayClient: activeDay,
	})
	return automation, db
}

// The one-per-day lock: the first claim of a local day owns it, the second is
// skipped without touching the upstream, and force bypasses the lock.
func TestActiveDayClaimGate(t *testing.T) {
	ctx := context.Background()
	turns := 0
	automation, db := newTestAutomation(t, func(context.Context, string, string) error {
		turns++
		return nil
	})

	first, err := automation.RunActiveDay(ctx, Provider, "cb-test", false)
	if err != nil {
		t.Fatalf("first run: %v", err)
	}
	if first.Status != "pending_confirmation" {
		t.Fatalf("first run status = %q, want pending_confirmation", first.Status)
	}
	if turns != 1 {
		t.Fatalf("turns after first run = %d, want 1", turns)
	}

	second, err := automation.RunActiveDay(ctx, Provider, "cb-test", false)
	if err != nil {
		t.Fatalf("second run: %v", err)
	}
	if second.Status != "skipped" || second.Detail != "already claimed today" {
		t.Fatalf("second run = %+v, want skipped/already claimed today", second)
	}
	if turns != 1 {
		t.Fatalf("turns after skipped run = %d, want 1 (no second ACP turn)", turns)
	}

	forced, err := automation.RunActiveDay(ctx, Provider, "cb-test", true)
	if err != nil {
		t.Fatalf("forced run: %v", err)
	}
	if forced.Status != "succeeded" {
		t.Fatalf("forced run status = %q, want succeeded", forced.Status)
	}
	if turns != 2 {
		t.Fatalf("turns after forced run = %d, want 2 (force spends a real turn)", turns)
	}

	row, err := db.GetActiveDay(ctx, Provider, "cb-test", automation.localDate(), "Asia/Shanghai")
	if err != nil {
		t.Fatalf("get active day: %v", err)
	}
	if row.Status != "succeeded" {
		t.Fatalf("row status = %q, want succeeded", row.Status)
	}
	// A forced rerun re-opens the confirmation, so the next confirm pass can
	// re-evaluate the day instead of short-circuiting on a stale verdict.
	if row.Confirmed != nil {
		t.Fatalf("confirmed after forced rerun = %v, want nil", *row.Confirmed)
	}
	if row.ConfirmAttempts != 0 {
		t.Fatalf("confirm attempts after forced rerun = %d, want 0", row.ConfirmAttempts)
	}
}

// The day-boundary guard: heatmap.today follows the upstream day boundary, so a
// cell for the local date is the only authoritative signal.
func TestTodayLitRequiresLocalDateCell(t *testing.T) {
	score := func(value float64) *float64 { return &value }
	cases := []struct {
		name     string
		overview Overview
		want     bool
	}{
		{
			name: "local cell with score lights the day",
			overview: Overview{Heatmap: Heatmap{Cells: []HeatmapCell{
				{Date: "2026-09-16", Score: score(2)},
			}}},
			want: true,
		},
		{
			name: "local cell with zero score does not",
			overview: Overview{Heatmap: Heatmap{Cells: []HeatmapCell{
				{Date: "2026-09-16", Score: score(0)},
			}}},
			want: false,
		},
		{
			name: "today is ignored when it reports another date",
			overview: Overview{Heatmap: Heatmap{
				Cells: []HeatmapCell{{Date: "2026-09-15", Score: score(4)}},
				Today: &HeatmapToday{Date: "2026-09-15", IsActive: true},
			}},
			want: false,
		},
		{
			name: "today is trusted when it carries the local date",
			overview: Overview{Heatmap: Heatmap{
				Today: &HeatmapToday{Date: "2026-09-16", IsActive: true},
			}},
			want: true,
		},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			if got := todayLit(testCase.overview, "2026-09-16"); got != testCase.want {
				t.Fatalf("todayLit = %v, want %v", got, testCase.want)
			}
		})
	}
}

// A day the upstream already credited must not spend a turn.
func TestActiveDaySkipsWhenUpstreamAlreadyLit(t *testing.T) {
	ctx := context.Background()
	turns := 0
	automation, db := newTestAutomation(t, func(context.Context, string, string) error {
		turns++
		return nil
	})
	score := 5.0
	localDate := automation.localDate()
	if _, err := db.ClaimActiveDay(ctx, store.ActiveDay{
		Provider: Provider, AccountID: "cb-test", LocalDate: localDate,
		Timezone: "Asia/Shanghai", Status: "running",
	}); err != nil {
		t.Fatalf("claim: %v", err)
	}
	// A second account run must still skip the turn when the official heatmap
	// already shows the day as lit.
	if _, err := db.UpsertAccount(ctx, store.Account{
		Provider: Provider, AccountID: "cb-lit", Source: "manual", Enabled: true,
	}); err != nil {
		t.Fatalf("seed second account: %v", err)
	}
	lit := automation.activeDay(ctx, "test-token", "cb-lit", localDate, "Asia/Shanghai",
		Overview{Heatmap: Heatmap{Cells: []HeatmapCell{{Date: localDate, Score: &score}}}}, true, false)
	if lit.Status != "skipped_external" {
		t.Fatalf("status = %q, want skipped_external", lit.Status)
	}
	if turns != 0 {
		t.Fatalf("turns = %d, want 0 (upstream already lit the day)", turns)
	}
	row, err := db.GetActiveDay(ctx, Provider, "cb-lit", localDate, "Asia/Shanghai")
	if err != nil {
		t.Fatalf("get row: %v", err)
	}
	if row.Confirmed == nil || *row.Confirmed != "lit" {
		t.Fatalf("confirmed = %v, want lit", row.Confirmed)
	}
}

// Confirmation must not call the upstream again once a verdict is recorded, and
// must not conclude a day before the attempt budget is spent.
func TestConfirmActiveDayAttemptBudget(t *testing.T) {
	ctx := context.Background()
	automation, db := newTestAutomation(t, nil)
	localDate := automation.localDate()
	timezone := "Asia/Shanghai"
	if _, err := db.ClaimActiveDay(ctx, store.ActiveDay{
		Provider: Provider, AccountID: "cb-test", LocalDate: localDate,
		Timezone: timezone, Status: "running",
	}); err != nil {
		t.Fatalf("claim: %v", err)
	}
	if err := db.FinishActiveDay(ctx, Provider, "cb-test", localDate, timezone, "succeeded", nil, ""); err != nil {
		t.Fatalf("finish: %v", err)
	}

	// Attempts 1 and 2 stay pending (budget is 3), attempt 3 concludes not_lit.
	for attempt, want := range []string{"pending", "pending", "not_lit"} {
		result, err := automation.ConfirmActiveDay(ctx, Provider, "cb-test")
		if err != nil {
			t.Fatalf("confirm %d: %v", attempt+1, err)
		}
		if result.Status != want {
			t.Fatalf("confirm %d status = %q, want %q", attempt+1, result.Status, want)
		}
	}
	// A concluded day is reported from the row, without another upstream read.
	settled, err := automation.ConfirmActiveDay(ctx, Provider, "cb-test")
	if err != nil {
		t.Fatalf("confirm after settle: %v", err)
	}
	if settled.Status != "confirmed" || settled.Detail != "not_lit" {
		t.Fatalf("settled = %+v, want confirmed/not_lit", settled)
	}
}

// A failed ACP turn must be recorded as failed with its safe code, and it must
// consume the day's claim so the next scheduled pass does not retry it.
func TestActiveDayFailureRecordsCode(t *testing.T) {
	ctx := context.Background()
	automation, db := newTestAutomation(t, func(context.Context, string, string) error {
		return &ActiveDayError{Code: "conversation_missing"}
	})
	result, err := automation.RunActiveDay(ctx, Provider, "cb-test", false)
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if result.Status != "failed" || result.Error != "conversation_missing" {
		t.Fatalf("result = %+v, want failed/conversation_missing", result)
	}
	row, err := db.GetActiveDay(ctx, Provider, "cb-test", automation.localDate(), "Asia/Shanghai")
	if err != nil {
		t.Fatalf("get row: %v", err)
	}
	if row.Status != "failed" || row.ErrorCode == nil || *row.ErrorCode != "conversation_missing" {
		t.Fatalf("row = %+v, want failed/conversation_missing", row)
	}
}

// A token that cannot be resolved must surface as the console's error code
// rather than an upstream call.
func TestActiveDayRequiresToken(t *testing.T) {
	ctx := context.Background()
	automation, _ := newTestAutomation(t, nil)
	if _, err := automation.RunActiveDay(ctx, Provider, "cb-missing", false); err == nil {
		t.Fatal("want an error for an unknown account")
	} else {
		var automationErr *AutomationError
		if !errors.As(err, &automationErr) || automationErr.Code != "account_not_found" {
			t.Fatalf("err = %v, want account_not_found", err)
		}
	}
}

// RunStep with an unknown step is a normal failed result, not an error: the
// console renders detail.
func TestRunStepUnknownStep(t *testing.T) {
	automation, _ := newTestAutomation(t, nil)
	result, err := automation.RunStep(context.Background(), Provider, "cb-test", "nope")
	if err != nil {
		t.Fatalf("err = %v, want nil", err)
	}
	if result.Status != "failed" || result.Detail != "unknown_step:nope" {
		t.Fatalf("result = %+v, want failed/unknown_step:nope", result)
	}
}

// The run payload must keep every step key with the console's status/detail
// shape, and the counters the scheduler sums.
func TestStepResultsPayloadShape(t *testing.T) {
	results := map[string]StepResult{
		StepTasks:     (StepResult{Status: "completed", Detail: "接受 1 个任务"}).WithExtra(map[string]any{"reward_credits": 3}),
		StepLottery:   skippedResult(),
		StepTravel:    {Status: "failed", Detail: "status_failed:transport:Error"},
		StepRedeem:    {Status: "insufficient", Detail: "连登 2/7 天"},
		StepBuddyOpen: {Status: "completed"},
		StepActiveDay: {Status: "pending_confirmation", Detail: "第 1 次 hy3 登录对话已完成，等待官方记账"},
	}
	payload := stepResultsPayload(results)
	if len(payload) != len(StepKeys) {
		t.Fatalf("payload keys = %d, want %d", len(payload), len(StepKeys))
	}
	for _, key := range StepKeys {
		entry, ok := payload[key].(map[string]any)
		if !ok {
			t.Fatalf("payload[%s] is not a map", key)
		}
		if _, ok := entry["status"].(string); !ok {
			t.Fatalf("payload[%s] has no status string: %#v", key, entry)
		}
		if _, ok := entry["detail"].(string); !ok {
			t.Fatalf("payload[%s] has no detail string: %#v", key, entry)
		}
	}
	tasks := payload[StepTasks].(map[string]any)
	if tasks["reward_credits"] != 3 {
		t.Fatalf("tasks reward_credits = %v, want 3", tasks["reward_credits"])
	}
	if got := results[StepTasks].Credits(); got != 3 {
		t.Fatalf("Credits() = %d, want 3", got)
	}
	if got := results[StepLottery].Credits(); got != 0 {
		t.Fatalf("skipped Credits() = %d, want 0", got)
	}
}

// A stored run must round-trip through the log table with the counters intact.
func TestRecordRunRoundTrip(t *testing.T) {
	ctx := context.Background()
	automation, db := newTestAutomation(t, nil)
	results := map[string]StepResult{
		StepTasks: (StepResult{Status: "completed", Detail: "接受 2 个任务，领取 1 个奖励，获得 5 积分"}).
			WithExtra(map[string]any{"accepted": 2, "claimed": 1, "reward_credits": 5}),
	}
	if err := automation.RecordRun(ctx, Provider, "cb-test", "manual", results); err != nil {
		t.Fatalf("record: %v", err)
	}
	entries, err := db.ListGrowthLog(ctx, Provider, "cb-test", 10, 0)
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(entries) != 1 {
		t.Fatalf("entries = %d, want 1", len(entries))
	}
	entry := entries[0]
	if entry.TriggeredBy != "manual" {
		t.Fatalf("triggered_by = %q, want manual", entry.TriggeredBy)
	}
	tasks, ok := entry.Results[StepTasks].(map[string]any)
	if !ok {
		t.Fatalf("results[tasks] = %#v", entry.Results[StepTasks])
	}
	if tasks["detail"] != "接受 2 个任务，领取 1 个奖励，获得 5 积分" {
		t.Fatalf("detail = %v", tasks["detail"])
	}
	if numericInt(tasks["accepted"]) != 2 || numericInt(tasks["reward_credits"]) != 5 {
		t.Fatalf("counters lost: %#v", tasks)
	}
	// created_at must be a UTC instant the console can render without shifting
	// it: formatBeijing parses a suffixed value as-is and treats a suffix-free
	// value as UTC, so either is safe as long as the zone is unambiguous.
	parsed, ok := store.ParseISO(entry.CreatedAt)
	if !ok {
		t.Fatalf("created_at = %q, want a parseable timestamp", entry.CreatedAt)
	}
	if _, offset := parsed.Zone(); offset != 0 {
		t.Fatalf("created_at = %q, want a UTC instant", entry.CreatedAt)
	}
}

// The overview payload must carry the exact key names the console types, plus
// the local active-day view.
func TestOverviewJSONShape(t *testing.T) {
	score := 4.0
	overview := Overview{
		Profile: map[string]any{"level": 3.0, "completed": 1.0, "total": 5.0, "max_level": false, "first_visit": false},
		Tasks: []Task{{
			TaskCode: "t1", Title: "标题", AcceptStatus: "not_accepted",
			ProgressCur: &score, HasReward: true,
		}},
		Heatmap: Heatmap{
			Cells:     []HeatmapCell{{Date: "2026-09-16", Score: &score, HasNewBuddy: true}},
			Today:     &HeatmapToday{Date: "2026-09-16", Score: &score, IsActive: true, StatusText: "已点亮"},
			UpdatedAt: "2026-09-16T00:00:00+00:00",
		},
		Streak:  Streak{Days: intPtr(4.0), NextTier: "7d"},
		Lottery: Lottery{AvailableChances: intPtr(2.0), TotalDraws: intPtr(9.0)},
	}
	payload := overview.JSON()
	for _, key := range []string{"profile", "tasks", "heatmap", "streak", "lottery"} {
		if _, ok := payload[key]; !ok {
			t.Fatalf("payload is missing %q", key)
		}
	}
	heatmap := payload["heatmap"].(map[string]any)
	for _, key := range []string{"cells", "today", "range_start", "range_end", "updated_at"} {
		if _, ok := heatmap[key]; !ok {
			t.Fatalf("heatmap is missing %q", key)
		}
	}
	task := payload["tasks"].([]map[string]any)[0]
	for _, key := range []string{
		"task_code", "title", "task_desc", "task_type", "tag", "accept_status",
		"progress_current", "progress_target", "reward_credit", "reward_energy",
		"has_reward", "reward_claimed", "claimed", "is_claimed", "receive_status",
		"locked", "is_new", "icon_url",
	} {
		if _, ok := task[key]; !ok {
			t.Fatalf("task is missing %q", key)
		}
	}
	streak := payload["streak"].(map[string]any)
	for _, key := range []string{
		"days", "next_tier", "next_tier_remaining", "makeup_balance", "makeup_max",
		"remaining_days", "timezone",
	} {
		if _, ok := streak[key]; !ok {
			t.Fatalf("streak is missing %q", key)
		}
	}
}

// ---- step derivation from the upstream payload ----

// The task predicates decide both what gets accepted and what gets claimed, so
// they are the whole of the tasks step's behaviour.
func TestTaskPredicates(t *testing.T) {
	current := func(value float64) *float64 { return &value }
	cases := []struct {
		name    string
		task    Task
		done    bool
		claimed bool
	}{
		{
			name: "accepted but unfinished is neither done nor claimed",
			task: Task{AcceptStatus: "accepted", ProgressCur: current(1), ProgressTgt: current(3)},
		},
		{
			name: "completed is done and claimable",
			task: Task{AcceptStatus: "completed", HasReward: true},
			done: true,
		},
		{
			name: "claimed is done and already rewarded",
			task: Task{AcceptStatus: "claimed"},
			done: true, claimed: true,
		},
		{
			name: "progress reaching the target is done",
			task: Task{AcceptStatus: "in_progress", ProgressCur: current(3), ProgressTgt: current(3)},
			done: true,
		},
		{
			name: "receive_status received counts as claimed",
			task: Task{AcceptStatus: "completed", ReceiveStatus: "Received"},
			done: true, claimed: true,
		},
		{
			name: "is_claimed flag counts as claimed",
			task: Task{AcceptStatus: "completed", IsClaimed: true},
			done: true, claimed: true,
		},
		{
			name: "accept_status casing is ignored",
			task: Task{AcceptStatus: " Not_Accepted "},
		},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			if got := taskDone(testCase.task); got != testCase.done {
				t.Fatalf("taskDone = %v, want %v", got, testCase.done)
			}
			if got := rewardAlreadyClaimed(testCase.task); got != testCase.claimed {
				t.Fatalf("rewardAlreadyClaimed = %v, want %v", got, testCase.claimed)
			}
		})
	}
}

// Credit extraction drives the metrics-refresh decision, so the key order and
// the fallback both matter.
func TestExtractCredits(t *testing.T) {
	cases := []struct {
		name     string
		result   map[string]any
		fallback float64
		want     int
	}{
		{name: "top-level reward_credit", result: map[string]any{"reward_credit": 5.0}, want: 5},
		{name: "credits key", result: map[string]any{"credits": 7.0}, want: 7},
		{name: "nested reward", result: map[string]any{"reward": map[string]any{"credit": 3.0}}, want: 3},
		{name: "zero is not a credit", result: map[string]any{"reward_credit": 0.0}, fallback: 4, want: 4},
		{name: "fallback used when absent", result: map[string]any{}, fallback: 9, want: 9},
		{name: "nothing available", result: map[string]any{}, want: 0},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			if got := extractCredits(testCase.result, testCase.fallback); got != testCase.want {
				t.Fatalf("extractCredits = %d, want %d", got, testCase.want)
			}
		})
	}
}

// A missing daily_limit_reached flag must not be read as permission to depart.
func TestTravelStateDerivation(t *testing.T) {
	cases := []struct {
		name   string
		status map[string]any
		want   string
	}{
		{name: "traveling is skipped", status: map[string]any{"state": "traveling"}, want: "skipped"},
		{name: "limit reached is reported", status: map[string]any{"daily_limit_reached": true}, want: "daily_limit_reached"},
		{name: "missing limit flag is not a departure", status: map[string]any{}, want: "daily_limit_reached"},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			server := newFakeGrowthServer(t, map[string]map[string]any{
				"/activity/growth/buddy/travel/status": testCase.status,
			})
			automation, _ := newTestAutomation(t, nil)
			automation.opts.GrowthClient = NewClient(server.URL, 0)
			result := automation.stepTravel(context.Background(), "token")
			if result.Status != testCase.want {
				t.Fatalf("status = %q, want %q", result.Status, testCase.want)
			}
		})
	}
}

// Redeem must refuse an unknown tier, report an unreached streak, and only spend
// the redemption when the tier is unlocked.
func TestRedeemDerivation(t *testing.T) {
	cases := []struct {
		name     string
		tier     string
		summary  map[string]any
		want     string
		extraKey string
		extraVal int
	}{
		{
			name: "unknown tier", tier: "30d",
			want: "failed",
		},
		{
			name: "streak too short", tier: "7d",
			summary: map[string]any{"remaining_days": 3.0},
			want:    "insufficient", extraKey: "required_days", extraVal: 7,
		},
		{
			name: "tier locked", tier: "7d",
			summary: map[string]any{"remaining_days": 10.0, "starter_status": "locked"},
			want:    "skipped",
		},
		{
			name: "tier unlocked", tier: "7d",
			summary: map[string]any{"remaining_days": 10.0, "starter_status": "unlocked"},
			want:    "completed",
		},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			server := newFakeGrowthServer(t, map[string]map[string]any{
				"/activity/growth/redeem/summary": testCase.summary,
				"/activity/growth/redeem":         map[string]any{},
			})
			automation, _ := newTestAutomation(t, nil)
			automation.opts.GrowthClient = NewClient(server.URL, 0)
			automation.opts.Runtime = &fakeRuntime{tier: testCase.tier}
			result := automation.stepRedeem(context.Background(), "token")
			if result.Status != testCase.want {
				t.Fatalf("status = %q, want %q (detail %q)", result.Status, testCase.want, result.Detail)
			}
			if testCase.extraKey != "" && numericInt(result.Extra[testCase.extraKey]) != testCase.extraVal {
				t.Fatalf("%s = %v, want %d", testCase.extraKey, result.Extra[testCase.extraKey], testCase.extraVal)
			}
		})
	}
}

// buddy_open reports the affordable count in its detail, not the clamped request.
func TestBuddyOpenReportsAffordable(t *testing.T) {
	server := newFakeGrowthServer(t, map[string]map[string]any{
		"/activity/growth/buddy/quota": map[string]any{"affordable": 4.0, "max_open_count": 2.0},
		"/activity/growth/buddy/open":  map[string]any{},
	})
	automation, _ := newTestAutomation(t, nil)
	automation.opts.GrowthClient = NewClient(server.URL, 0)
	result := automation.stepBuddyOpen(context.Background(), "token")
	if result.Status != "completed" {
		t.Fatalf("status = %q, want completed", result.Status)
	}
	if result.Detail != "抽取了 4 个 Buddy" {
		t.Fatalf("detail = %q, want the affordable count", result.Detail)
	}
}

// Lottery with no chances is a skip, and the draw count is capped.
func TestLotteryDerivation(t *testing.T) {
	server := newFakeGrowthServer(t, map[string]map[string]any{
		"/activity/growth/lottery/draw": map[string]any{"reward_credit": 2.0},
	})
	automation, _ := newTestAutomation(t, nil)
	automation.opts.GrowthClient = NewClient(server.URL, 0)

	chances := 0
	none := automation.stepLottery(context.Background(), "token", Overview{
		Lottery: Lottery{AvailableChances: &chances},
	})
	if none.Status != "no_chances" {
		t.Fatalf("status = %q, want no_chances", none.Status)
	}

	chances = 25
	drawn := automation.stepLottery(context.Background(), "token", Overview{
		Lottery: Lottery{AvailableChances: &chances},
	})
	if drawn.Status != "completed" {
		t.Fatalf("status = %q, want completed", drawn.Status)
	}
	if numericInt(drawn.Extra["drawn"]) != 10 {
		t.Fatalf("drawn = %v, want the cap of 10", drawn.Extra["drawn"])
	}
	if numericInt(drawn.Extra["reward_credits"]) != 20 {
		t.Fatalf("reward_credits = %v, want 20", drawn.Extra["reward_credits"])
	}
}

// A step that panics becomes a failed result instead of aborting the run.
func TestGuardConvertsPanic(t *testing.T) {
	automation, _ := newTestAutomation(t, nil)
	result := automation.guard(func() StepResult { panic("boom") })
	if result.Status != "failed" || !strings.Contains(result.Detail, "boom") {
		t.Fatalf("result = %+v, want a failed result carrying the panic", result)
	}
}

// Disabled steps must stay in the payload as skipped/未启用 rather than vanish.
func TestDisabledStepsRemainSkipped(t *testing.T) {
	server := newFakeGrowthServer(t, map[string]map[string]any{
		"/v2/activity/growth/profile":      map[string]any{},
		"/v2/activity/growth/tasks":        map[string]any{"tasks": []any{}},
		"/activity/growth/heatmap":         map[string]any{"cells": []any{}},
		"/activity/growth/streak":          map[string]any{},
		"/activity/growth/lottery/summary": map[string]any{},
	})
	automation, _ := newTestAutomation(t, nil)
	automation.opts.GrowthClient = NewClient(server.URL, 0)
	automation.settings.GrowthAutoTasks = false
	automation.settings.GrowthAutoLottery = false
	automation.settings.GrowthAutoTravel = false
	automation.settings.GrowthAutoRedeem = false
	automation.settings.GrowthAutoBuddyOpen = false
	automation.settings.GrowthAutoActiveDay = false

	results := automation.RunSteps(context.Background(), "token", "cb-test", automation.localDate(), "Asia/Shanghai")
	if len(results) != len(StepKeys) {
		t.Fatalf("results = %d, want %d", len(results), len(StepKeys))
	}
	for _, key := range StepKeys {
		if results[key].Status != "skipped" || results[key].Detail != "未启用" {
			t.Fatalf("%s = %+v, want skipped/未启用", key, results[key])
		}
	}
}

// A failed overview read must surface on every step instead of silently
// reporting the disabled placeholder.
func TestFetchFailurePropagatesToSteps(t *testing.T) {
	automation, _ := newTestAutomation(t, nil)
	// Port 1 on the loopback interface refuses connections immediately.
	automation.opts.GrowthClient = NewClient("http://127.0.0.1:1", 0)
	automation.settings.GrowthAutoTasks = true
	results := automation.RunSteps(context.Background(), "token", "cb-test", automation.localDate(), "Asia/Shanghai")
	tasks := results[StepTasks]
	if tasks.Status != "skipped" || !strings.HasPrefix(tasks.Error, "fetch_failed:") {
		t.Fatalf("tasks = %+v, want skipped with fetch_failed", tasks)
	}
}

// fakeRuntime overrides growth settings for a test. autoFlags is the value
// every auto_* accessor reports, so a test can assert an override wins over a
// contradicting environment default.
type fakeRuntime struct {
	tier      string
	attempts  int
	autoFlags bool
}

func (f *fakeRuntime) GrowthSchedulerEnabled() bool        { return true }
func (f *fakeRuntime) GrowthSchedulerIntervalSeconds() int { return 1800 }
func (f *fakeRuntime) GrowthAutoTasks() bool               { return f.autoFlags }
func (f *fakeRuntime) GrowthAutoLottery() bool             { return f.autoFlags }
func (f *fakeRuntime) GrowthAutoTravel() bool              { return f.autoFlags }
func (f *fakeRuntime) GrowthAutoRedeem() bool              { return f.autoFlags }
func (f *fakeRuntime) GrowthAutoBuddyOpen() bool           { return f.autoFlags }
func (f *fakeRuntime) GrowthAutoActiveDay() bool           { return f.autoFlags }
func (f *fakeRuntime) GrowthRedeemTier() string            { return f.tier }
func (f *fakeRuntime) GrowthActiveDayConfirmAttempts() int { return f.attempts }
