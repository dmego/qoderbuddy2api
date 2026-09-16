package growth

import (
	"context"
	"fmt"
	"strings"
)

// stepTasks accepts every pending task and claims every completed reward that
// has not been claimed yet.
func (a *Automation) stepTasks(ctx context.Context, token string, overview Overview) StepResult {
	tasks := overview.Tasks
	accepted := a.acceptPending(ctx, token, tasks)
	claimed, credits := a.claimCompleted(ctx, token, tasks)
	detail := fmt.Sprintf("接受 %d 个任务，领取 %d 个奖励", accepted, claimed)
	if credits > 0 {
		detail += fmt.Sprintf("，获得 %d 积分", credits)
	}
	return StepResult{Status: "completed", Detail: detail}.WithExtra(map[string]any{
		"accepted":       accepted,
		"claimed":        claimed,
		"reward_credits": credits,
	})
}

// acceptPending accepts the tasks that are pending and unlocked. A rejection
// counts as zero accepted instead of failing the step, because the other tasks
// were still worth attempting.
func (a *Automation) acceptPending(ctx context.Context, token string, tasks []Task) int {
	pending := make([]string, 0, len(tasks))
	for _, task := range tasks {
		if taskStatus(task) != "not_accepted" || task.Locked || task.TaskCode == "" {
			continue
		}
		pending = append(pending, task.TaskCode)
	}
	if len(pending) == 0 {
		return 0
	}
	if _, err := a.opts.GrowthClient.AcceptTasks(ctx, token, pending); err != nil {
		return 0
	}
	return len(pending)
}

// claimCompleted claims the rewards of finished tasks. One failed claim does not
// stop the others.
func (a *Automation) claimCompleted(ctx context.Context, token string, tasks []Task) (int, int) {
	claimed := 0
	credits := 0
	for _, task := range tasks {
		if !taskDone(task) || !task.HasReward || rewardAlreadyClaimed(task) || task.TaskCode == "" {
			continue
		}
		result, err := a.opts.GrowthClient.ClaimTask(ctx, token, task.TaskCode)
		if err != nil {
			continue
		}
		claimed++
		credits += extractCredits(result, task.RewardCredit)
	}
	return claimed, credits
}

// stepLottery draws the available chances, capped so one run cannot spend an
// unbounded number of draws.
func (a *Automation) stepLottery(ctx context.Context, token string, overview Overview) StepResult {
	chances := 0
	if overview.Lottery.AvailableChances != nil {
		chances = *overview.Lottery.AvailableChances
	}
	if chances == 0 {
		return StepResult{Status: "no_chances", Detail: "暂无抽奖次数"}.WithExtra(map[string]any{
			"drawn": 0, "available": 0,
		})
	}
	drawn := 0
	credits := 0
	for range min(chances, 10) {
		result, err := a.opts.GrowthClient.LotteryDraw(ctx, token)
		if err != nil {
			// Credits already won by earlier draws are kept.
			break
		}
		drawn++
		credits += extractCredits(result, 0)
	}
	detail := fmt.Sprintf("抽奖 %d/%d 次", drawn, chances)
	if credits > 0 {
		detail += fmt.Sprintf("，获得 %d 积分", credits)
	}
	return StepResult{Status: "completed", Detail: detail}.WithExtra(map[string]any{
		"drawn":          drawn,
		"available":      chances,
		"reward_credits": credits,
	})
}

// stepTravel runs one buddy trip: claim a finished trip, otherwise depart when
// the daily limit still allows it.
func (a *Automation) stepTravel(ctx context.Context, token string) StepResult {
	status, err := a.opts.GrowthClient.TravelStatus(ctx, token)
	if err != nil {
		return StepResult{Status: "failed", Detail: "status_failed:" + unavailableCode(err)}
	}
	state := stringField(status["state"])
	switch {
	case state == "arrived":
		return a.claimTravel(ctx, token)
	case state == "traveling":
		return StepResult{Status: "skipped", Detail: "Buddy 正在旅行中"}
	}
	// Only an explicit boolean false means the limit is still open: a missing
	// field must not be read as permission to depart.
	if value, ok := status["daily_limit_reached"].(bool); ok && !value {
		return a.departTravel(ctx, token)
	}
	return StepResult{Status: "daily_limit_reached", Detail: "今日旅行次数已用完"}
}

func (a *Automation) claimTravel(ctx context.Context, token string) StepResult {
	result, err := a.opts.GrowthClient.TravelClaim(ctx, token)
	if err != nil {
		return StepResult{Status: "failed", Detail: "claim_failed:" + unavailableCode(err)}
	}
	credits := extractCredits(result, 0)
	detail := "旅行已结束，已领取奖励"
	if credits > 0 {
		detail += fmt.Sprintf("，获得 %d 积分", credits)
	}
	return StepResult{Status: "completed", Detail: detail}.WithExtra(map[string]any{
		"reward_credits": credits,
	})
}

func (a *Automation) departTravel(ctx context.Context, token string) StepResult {
	config, err := a.opts.GrowthClient.TravelConfig(ctx, token)
	if err != nil {
		return StepResult{Status: "failed", Detail: "depart_failed:" + unavailableCode(err)}
	}
	locations, _ := config["locations"].([]any)
	if len(locations) == 0 {
		return StepResult{Status: "skipped", Detail: "暂无可用旅行地点"}
	}
	first, _ := locations[0].(map[string]any)
	locationID, ok := integerField(first["id"])
	if !ok {
		return StepResult{Status: "failed", Detail: "invalid_location"}
	}
	if _, err := a.opts.GrowthClient.TravelDepart(ctx, token, locationID); err != nil {
		return StepResult{Status: "failed", Detail: "depart_failed:" + unavailableCode(err)}
	}
	name := stringField(first["name"])
	if name == "" {
		name = "未知地点"
	}
	return StepResult{Status: "completed", Detail: "Buddy 已出发前往 " + name}
}

// stepRedeem redeems the configured consecutive-day tier once it is unlocked.
func (a *Automation) stepRedeem(ctx context.Context, token string) StepResult {
	tier := a.redeemTier()
	if tier == "off" {
		return StepResult{Status: "skipped", Detail: "兑换已关闭"}
	}
	required, known := redeemDays[tier]
	if !known {
		return StepResult{Status: "failed", Detail: "unknown_tier:" + tier}
	}
	summary, err := a.opts.GrowthClient.RedeemSummary(ctx, token)
	if err != nil {
		return StepResult{Status: "failed", Detail: "summary_failed:" + unavailableCode(err)}
	}
	remaining, _ := integerField(summary["remaining_days"])
	if remaining < required {
		return StepResult{
			Status: "insufficient",
			Detail: fmt.Sprintf("连登 %d/%d 天，还差 %d 天", remaining, required, required-remaining),
		}.WithExtra(map[string]any{
			"remaining_days": remaining,
			"required_days":  required,
		})
	}
	if stringField(summary[redeemTierStatusKey[tier]]) != "unlocked" {
		return StepResult{Status: "skipped", Detail: "档位 " + tier + " 尚未解锁"}
	}
	if _, err := a.opts.GrowthClient.Redeem(ctx, token, tier); err != nil {
		return StepResult{Status: "failed", Detail: "redeem_failed:" + unavailableCode(err)}
	}
	return StepResult{Status: "completed", Detail: "已兑换 " + tier + " 档奖励"}.WithExtra(map[string]any{
		"tier": tier,
	})
}

// stepBuddyOpen spends the affordable energy on buddy draws.
func (a *Automation) stepBuddyOpen(ctx context.Context, token string) StepResult {
	quota, err := a.opts.GrowthClient.BuddyQuota(ctx, token)
	if err != nil {
		return StepResult{Status: "failed", Detail: "quota_failed:" + unavailableCode(err)}
	}
	affordable, _ := integerField(quota["affordable"])
	if affordable == 0 {
		return StepResult{Status: "skipped", Detail: "能量不足，无法抽取 Buddy"}
	}
	maxOpen, _ := integerField(quota["max_open_count"])
	if maxOpen == 0 {
		maxOpen = 1
	}
	if _, err := a.opts.GrowthClient.BuddyOpen(ctx, token, min(affordable, maxOpen)); err != nil {
		return StepResult{Status: "failed", Detail: "open_failed:" + unavailableCode(err)}
	}
	// The detail reports the affordable count rather than the clamped request,
	// so the displayed number matches what the account actually paid for.
	return StepResult{
		Status: "completed",
		Detail: fmt.Sprintf("抽取了 %d 个 Buddy", affordable),
	}.WithExtra(map[string]any{"opened": affordable})
}

// ---- task predicates ----

// taskStatus is the normalised accept_status the predicates compare against.
func taskStatus(task Task) string {
	return strings.ToLower(strings.TrimSpace(task.AcceptStatus))
}

// taskDone reports whether a task's goal is met.
func taskDone(task Task) bool {
	switch taskStatus(task) {
	case "completed", "claimed":
		return true
	}
	if task.ProgressCur != nil && task.ProgressTgt != nil {
		return *task.ProgressCur >= *task.ProgressTgt
	}
	return false
}

// rewardAlreadyClaimed reports whether the task's reward was already taken, from
// any of the four flags the upstream uses.
func rewardAlreadyClaimed(task Task) bool {
	if taskStatus(task) == "claimed" {
		return true
	}
	if task.RewardClaimed || task.Claimed || task.IsClaimed {
		return true
	}
	receive := strings.ToLower(strings.TrimSpace(task.ReceiveStatus))
	return receive == "claimed" || receive == "received"
}

// extractCredits reads the credit amount out of a mutation response, falling
// back to the task's advertised reward. The key order mirrors the upstream
// payloads the Python build drilled through.
func extractCredits(result map[string]any, fallback float64) int {
	for _, key := range []string{"reward_credit", "credits", "credit", "reward_credits", "amount"} {
		if value, ok := positiveNumber(result[key]); ok {
			return int(value)
		}
	}
	if reward, ok := result["reward"].(map[string]any); ok {
		for _, key := range []string{"credit", "credits", "amount"} {
			if value, ok := positiveNumber(reward[key]); ok {
				return int(value)
			}
		}
	}
	if fallback > 0 {
		return int(fallback)
	}
	return 0
}

// positiveNumber accepts a JSON number greater than zero.
func positiveNumber(value any) (float64, bool) {
	parsed := numberPtr(value)
	if parsed == nil || *parsed <= 0 {
		return 0, false
	}
	return *parsed, true
}

// numericInt reads an integer out of a persisted step counter.
func numericInt(value any) int {
	parsed := numberPtr(value)
	if parsed == nil {
		return 0
	}
	return int(*parsed)
}

// integerField accepts a JSON number that has no fractional part. Go decodes
// every JSON number as a float64, so this is the equivalent of the Python
// isinstance(value, int) check the upstream contract relies on.
func integerField(value any) (int, bool) {
	parsed := numberPtr(value)
	if parsed == nil || *parsed != float64(int(*parsed)) {
		return 0, false
	}
	return int(*parsed), true
}
