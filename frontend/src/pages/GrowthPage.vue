<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import {
  CalendarDays, CheckCircle, ChevronDown, ChevronUp, Dice5, Gift, Globe, LogIn, MapPin, PawPrint,
  Play, RefreshCcw, Sparkles, Sprout, Trophy,
} from "@lucide/vue";
import { computed, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { apiRequest } from "@/api/client";
import { formatBeijing } from "@/utils/format";
import NotificationRegion from "@/components/NotificationRegion.vue";
import PanelHeader from "@/components/PanelHeader.vue";
import StatePill from "@/components/StatePill.vue";
import { useNotifications } from "@/composables/useNotifications";

type Account = { provider: string; account_id: string; label: string; source: string };
type AccountPage = { accounts: Account[]; next_cursor?: string | null };
type GrowthProfile = { level?: number | null; completed?: number | null; total?: number | null; max_level?: boolean | null };
type GrowthTask = { task_code?: string; title?: string; task_desc?: string; task_type?: string; tag?: string; accept_status?: string; progress_current?: number | null; progress_target?: number | null; reward_credit?: number | null; reward_energy?: number | null; has_reward?: boolean | null; reward_claimed?: boolean | null; claimed?: boolean | null; is_claimed?: boolean | null; receive_status?: string; locked?: boolean | null; is_new?: boolean | null; icon_url?: string | null };
type HeatmapCell = { date?: string; score?: number | null; has_new_buddy?: boolean | null };
type ActiveDayLocal = { local_date?: string | null; status?: string | null; error_code?: string | null; confirmed?: string | null; confirm_attempts?: number | null; finished_at?: string | null };
type GrowthOverview = { profile: GrowthProfile; tasks: GrowthTask[]; heatmap: { cells: HeatmapCell[]; today?: { date?: string; score?: number | null; is_active?: boolean | null; status_text?: string } | null; range_start?: string | null; range_end?: string | null }; streak: { days?: number | null; next_tier?: string | null; next_tier_remaining?: number | null; makeup_balance?: number | null; makeup_max?: number | null; remaining_days?: number | null; timezone?: string | null }; lottery: { available_chances?: number | null; total_draws?: number | null }; active_day_local?: ActiveDayLocal | null };
type StepKey = "tasks" | "lottery" | "travel" | "redeem" | "buddy_open" | "active_day";
type StepResult = { status: string; detail: string; [key: string]: unknown };
type HistoryEntry = { id: number; created_at: string; triggered_by: string; results: Partial<Record<StepKey, StepResult>> };

const route = useRoute();
const router = useRouter();
const queryClient = useQueryClient();
const { notifications, notify, dismiss } = useNotifications();

const selectedAccountKey = ref(String(route.query.account ?? ""));
const runningStep = ref<StepKey | "all" | null>(null);

const accounts = useQuery({
  queryKey: ["growth-accounts"],
  queryFn: async (): Promise<AccountPage> => {
    const all: Account[] = [];
    let cursor = "";
    do {
      const ep = `/accounts?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`;
      const page: AccountPage = await apiRequest<AccountPage>(ep);
      all.push(...page.accounts.filter((a) => a.provider === "codebuddy" && a.source !== "env"));
      cursor = page.next_cursor ?? "";
    } while (cursor);
    return { accounts: all, next_cursor: null };
  },
  staleTime: 30_000,
});

const codebuddyAccounts = computed(() => accounts.data.value?.accounts ?? []);
const effectiveAccountKey = computed(() => selectedAccountKey.value || (codebuddyAccounts.value[0] ? `${codebuddyAccounts.value[0].provider}:${codebuddyAccounts.value[0].account_id}` : ""));

watch(codebuddyAccounts, (list) => {
  if (!selectedAccountKey.value && list.length) {
    selectedAccountKey.value = `${list[0].provider}:${list[0].account_id}`;
  }
}, { immediate: true });

const selectedParts = computed(() => effectiveAccountKey.value.split(":"));
const provider = computed(() => selectedParts.value[0] || "codebuddy");
const accountId = computed(() => selectedParts.value[1] || "");
const base = computed(() => `/accounts/${encodeURIComponent(provider.value)}/${encodeURIComponent(accountId.value)}`);

const growth = useQuery({
  queryKey: ["account-growth", provider, accountId],
  enabled: computed(() => Boolean(accountId.value)),
  queryFn: () => apiRequest<GrowthOverview>(`${base.value}/growth`),
  staleTime: 30_000,
});

const historyPage = ref(1);
const historyPageSize = 10;
const history = useQuery({
  queryKey: ["account-growth-history", provider, accountId, historyPage],
  enabled: computed(() => Boolean(accountId.value)),
  queryFn: () => apiRequest<{ logs: HistoryEntry[]; total: number; page: number; pages: number }>(`${base.value}/growth/history?page=${historyPage.value}&page_size=${historyPageSize}`),
  staleTime: 15_000,
});
const historyPages = computed(() => history.data.value?.pages ?? 0);
const historyTotal = computed(() => history.data.value?.total ?? 0);
function setHistoryPage(delta: number): void {
  historyPage.value = Math.min(Math.max(1, historyPage.value + delta), Math.max(1, historyPages.value));
}
// 卡片"上次"展示用最近 20 条，避免翻页后失效。
const historyLatest = useQuery({
  queryKey: ["account-growth-history-latest", provider, accountId],
  enabled: computed(() => Boolean(accountId.value)),
  queryFn: () => apiRequest<{ logs: HistoryEntry[] }>(`${base.value}/growth/history?page_size=20`),
  staleTime: 15_000,
});

const settings = useQuery({
  queryKey: ["growth-settings"],
  queryFn: () => apiRequest<{ settings: { key: string; value: unknown; value_version: number }[] }>("/settings"),
  staleTime: 30_000,
});

const settingValue = (key: string) => settings.data.value?.settings.find((i) => i.key === key)?.value;

const stepConfigs = computed(() => [
  { key: "active_day" as StepKey, label: "登录自动化", icon: LogIn, enabled: Boolean(settingValue("growth.auto_active_day")) },
  { key: "tasks" as StepKey, label: "任务自动化", icon: Sparkles, enabled: Boolean(settingValue("growth.auto_tasks")) },
  { key: "lottery" as StepKey, label: "抽奖自动化", icon: Dice5, enabled: Boolean(settingValue("growth.auto_lottery")) },
  { key: "travel" as StepKey, label: "旅行自动化", icon: MapPin, enabled: Boolean(settingValue("growth.auto_travel")) },
  { key: "redeem" as StepKey, label: "兑换自动化", icon: Gift, enabled: Boolean(settingValue("growth.auto_redeem")), hasTier: true },
  { key: "buddy_open" as StepKey, label: "Buddy 自动化", icon: PawPrint, enabled: Boolean(settingValue("growth.auto_buddy_open")) },
]);

const lastResults = computed(() => {
  const logs = historyLatest.data.value?.logs ?? [];
  if (!logs.length) return {} as Record<StepKey, StepResult | null>;
  const latest = logs[0];
  return latest.results as Record<StepKey, StepResult | null>;
});

function lastStepResult(key: StepKey): StepResult | null {
  return lastResults.value[key] ?? null;
}

function findLatestStepLog(key: StepKey): { created_at: string; triggered_by: string; result: StepResult } | null {
  const logs = historyLatest.data.value?.logs ?? [];
  for (const log of logs) {
    const result = log.results[key];
    if (result) return { created_at: log.created_at, triggered_by: log.triggered_by, result };
  }
  return null;
}

const heatmapGrid = computed(() => {
  const cells = growth.data.value?.heatmap?.cells ?? [];
  const weeks: HeatmapCell[][] = [];
  for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7));
  return { weeks };
});

const activeTasks = computed(() => (growth.data.value?.tasks ?? []).filter((t) => taskStatus(t) !== "completed"));
const completedTasks = computed(() => (growth.data.value?.tasks ?? []).filter((t) => taskStatus(t) === "completed"));
const showCompleted = ref(false);

function cellLevel(cell: HeatmapCell): number {
  const s = cell.score ?? 0;
  if (s === 0) return 0;
  if (s <= 3) return 1;
  if (s <= 7) return 2;
  if (s <= 11) return 3;
  return 4;
}

function cellTitle(cell: HeatmapCell): string {
  const s = cell.score ?? 0;
  return `${cell.date} · ${s} 分${cell.has_new_buddy ? " · 获得 Buddy" : ""}`;
}

function taskStatus(task: GrowthTask): string {
  if (task.locked) return "locked";
  const status = task.accept_status?.trim().toLowerCase();
  if (status === "claimed" || task.reward_claimed || task.claimed || task.is_claimed || task.receive_status === "claimed" || task.receive_status === "received") return "completed";
  if (status === "completed") return task.has_reward ? "claimable" : "completed";
  if (status === "not_accepted") return "not_accepted";
  if (status === "accepted" || status === "in_progress") return "in_progress";
  const c = task.progress_current, t = task.progress_target;
  const done = typeof c === "number" && typeof t === "number" && c >= t;
  if (task.has_reward && done) return "claimable";
  if (done) return "completed";
  return "in_progress";
}

function taskStatusLabel(status: string): string {
  return { completed: "已完成", claimable: "可领奖", locked: "锁定中", not_accepted: "未接受", in_progress: "进行中" }[status] ?? status;
}

async function invalidateAll() {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: ["account-growth"] }),
    queryClient.invalidateQueries({ queryKey: ["account-growth-history"] }),
  ]);
}

const runStep = useMutation({
  mutationFn: async (step: StepKey) => {
    runningStep.value = step;
    return apiRequest<{ result: StepResult }>(`${base.value}/growth/run/${step}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  },
  onSuccess: async (data, step) => {
    notify(`${stepLabel(step)}执行完成`, { message: data.result.detail, tone: "success" });
    await invalidateAll();
  },
  onError: (error, step) => notify(`${stepLabel(step)}执行失败`, { message: String(error), tone: "error", timeout: 0 }),
  onSettled: () => { runningStep.value = null; },
});

const runAll = useMutation({
  mutationFn: async () => {
    runningStep.value = "all";
    return apiRequest<{ result: Record<string, StepResult> }>(`${base.value}/growth/execute`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  },
  onSuccess: async () => { notify("全部自动化执行完成", { tone: "success" }); await invalidateAll(); },
  onError: (error) => notify("自动化执行失败", { message: String(error), tone: "error", timeout: 0 }),
  onSettled: () => { runningStep.value = null; },
});

const rerunActiveDay = useMutation({
  mutationFn: async () => {
    runningStep.value = "active_day";
    return apiRequest<{ result: StepResult }>(`${base.value}/growth/active-day/rerun`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  },
  onSuccess: async (data) => { notify("今日活跃日重试完成", { message: data.result.detail ?? data.result.status, tone: "success" }); await invalidateAll(); },
  onError: (error) => notify("活跃日重试失败", { message: String(error), tone: "error", timeout: 0 }),
  onSettled: () => { runningStep.value = null; },
});

const toggleSetting = useMutation({
  mutationFn: async ({ key, value }: { key: string; value: boolean }) => {
    const current = settings.data.value?.settings.find((item) => item.key === key);
    return apiRequest(`/settings`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, value, value_version: current?.value_version ?? 0 }),
    });
  },
  onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: ["growth-settings"] }); },
  onError: (error) => notify("设置更新失败", { message: String(error), tone: "error" }),
});

function stepLabel(key: StepKey): string {
  return { active_day: "登录", tasks: "任务", lottery: "抽奖", travel: "旅行", redeem: "兑换", buddy_open: "Buddy" }[key];
}

function activeDayStatusLabel(local: ActiveDayLocal): string {
  const st = local.status ?? "";
  if (st === "succeeded") return `${local.confirmed === "lit" ? "已点亮" : local.confirmed === "not_lit" ? "未点亮·将自动重试" : "成功·待确认"}`;
  if (st === "skipped_external") return "当天已点亮·跳过";
  if (st === "failed") return `失败·${local.error_code ?? "?"}`;
  if (st === "running") return "执行中";
  return st || "未知";
}

function triggerLabel(trigger: string): string {
  if (trigger === "scheduler") return "调度器";
  if (trigger.startsWith("manual:")) return "手动";
  return trigger;
}

function stepTone(status: string): string {
  const s = status.toLowerCase();
  if (["succeeded", "completed", "claimed", "lit", "accepted", "ok"].includes(s)) return "ok";
  if (["failed", "error", "not_lit", "invalid", "unknown_step"].includes(s)) return "bad";
  if (["pending", "running", "in_progress"].includes(s)) return "run";
  return "skip";
}

function stepShort(status: string): string {
  const labels: Record<string, string> = {
    succeeded: "成功", completed: "完成", claimed: "已领", lit: "点亮",
    failed: "失败", not_lit: "未点亮", pending_confirmation: "待官方确认", error: "错误",
    pending: "待定", running: "执行中",
    skipped: "跳过", no_chances: "无次数", daily_limit_reached: "已用尽",
    insufficient: "条件不足", already_claimed: "已执行", disabled: "未启用",
    skip_irrelevant: "无关", unknown_step: "未知",
  };
  return labels[status.toLowerCase()] ?? status;
}

function stepDetail(result?: StepResult): string {
  return result ? (result.detail ?? result.status) : "--";
}

function confirmRerunActiveDay(): void {
  if (window.confirm("今日重试会再消耗一次 WorkBuddy 对话额度（真实扣费），确认重试？")) {
    rerunActiveDay.mutate();
  }
}

function navigateToAccount(): void {
  if (selectedAccountKey.value) {
    router.replace({ name: "growth", query: { account: selectedAccountKey.value } });
  }
}
</script>

<template>
  <section class="page-content growth-page">
    <header class="page-header">
      <div>
        <h1>成长中心</h1>
        <p>WorkBuddy 成长计划 · 任务 · 连登 · 抽奖 · 旅行 · 兑换</p>
      </div>
      <div class="header-actions">
        <select v-model="selectedAccountKey" aria-label="选择账号" @change="navigateToAccount">
          <option value="" disabled>选择账号…</option>
          <option v-for="acc in codebuddyAccounts" :key="`${acc.provider}:${acc.account_id}`" :value="`${acc.provider}:${acc.account_id}`">{{ acc.label }} · {{ acc.account_id }}</option>
        </select>
        <button class="secondary-button" type="button" :disabled="growth.isFetching.value || !accountId" @click="growth.refetch()"><RefreshCcw :class="{ spin: growth.isFetching.value }" :size="16" />刷新</button>
        <button type="button" :disabled="runningStep !== null || !accountId" @click="runAll.mutate()"><Play :size="16" />{{ runningStep === 'all' ? '执行中…' : '执行全部' }}</button>
      </div>
    </header>

    <div v-if="accounts.isPending.value" class="loading-row">正在读取账号列表…</div>
    <div v-else-if="!codebuddyAccounts.length" class="compact-empty">没有可用的 CodeBuddy 账号。</div>
    <template v-else-if="accountId">
      <div v-if="growth.isError.value" class="data-state data-state--warning">成长中心读取失败：{{ growth.error.value }}<button class="secondary-button compact-button" type="button" @click="growth.refetch()">重试</button></div>
      <template v-else-if="growth.data.value">
        <!-- 摘要卡片 -->
        <div class="summary-grid">
          <article class="summary-tile"><Sprout :size="18" /><span>成长等级</span><strong>{{ growth.data.value.profile?.level ?? '--' }}</strong><small>已完成 {{ growth.data.value.profile?.completed ?? '--' }}/{{ growth.data.value.profile?.total ?? '--' }}</small></article>
          <article class="summary-tile"><CalendarDays :size="18" /><span>连登天数</span><strong>{{ growth.data.value.streak?.days ?? "--" }}</strong><small v-if="growth.data.value.streak?.next_tier">距 {{ growth.data.value.streak.next_tier }} 还差 {{ growth.data.value.streak.next_tier_remaining ?? '--' }} 天</small><small v-else>已达最高档</small></article>
          <article class="summary-tile"><Dice5 :size="18" /><span>可抽奖</span><strong>{{ growth.data.value.lottery?.available_chances ?? 0 }}</strong><small>累计抽 {{ growth.data.value.lottery?.total_draws ?? 0 }} 次</small></article>
          <article class="summary-tile"><Trophy :size="18" /><span>补登卡</span><strong>{{ growth.data.value.streak?.makeup_balance ?? 0 }}/{{ growth.data.value.streak?.makeup_max ?? 4 }}</strong><small>可用于补登历史天数</small></article>
        </div>

        <!-- 自动化控制面板 -->
        <section class="data-panel">
          <PanelHeader title="自动化控制" description="每个功能独立开关，手动执行不受开关限制。" />
          <div class="automation-grid">
            <div v-for="step in stepConfigs" :key="step.key" class="automation-card" :class="{ 'automation-card--off': !step.enabled }">
              <div class="automation-card-header">
                <div class="automation-card-title">
                  <component :is="step.icon" :size="16" />
                  <strong>{{ step.label }}</strong>
                </div>
                <label class="switch" :aria-label="step.label">
                  <input type="checkbox" :checked="step.enabled" :disabled="toggleSetting.isPending.value" @change="toggleSetting.mutate({ key: `growth.auto_${step.key}`, value: !step.enabled })" />
                  <span></span>
                </label>
              </div>
              <div class="automation-card-body">
                <div v-if="lastStepResult(step.key)" class="automation-result">
                  <StatePill :value="lastStepResult(step.key)!.status" />
                  <span class="automation-detail">{{ lastStepResult(step.key)!.detail }}</span>
                </div>
                <div v-else class="automation-result automation-result--empty">
                  <span class="automation-detail">尚未执行</span>
                </div>
                <div class="automation-meta">
                  <small v-if="findLatestStepLog(step.key)">上次: {{ formatBeijing(findLatestStepLog(step.key)!.created_at) }} · {{ triggerLabel(findLatestStepLog(step.key)!.triggered_by) }}</small>
                  <small v-else>尚未执行</small>
                </div>
                <div v-if="step.key === 'active_day'" class="active-day-today">
                  <span class="active-day-chip">今日: {{ growth.data.value?.active_day_local ? activeDayStatusLabel(growth.data.value.active_day_local) : '尚未执行' }}</span>
                </div>
              </div>
              <div class="automation-card-footer">
                <button class="secondary-button compact-button" type="button" :disabled="runningStep !== null" @click="step.key === 'active_day' ? confirmRerunActiveDay() : runStep.mutate(step.key)">
                  <Play :size="14" />{{ runningStep === step.key ? '执行中…' : (step.key === 'active_day' ? '今日重试' : '手动执行') }}
                </button>
              </div>
            </div>
          </div>
        </section>

        <!-- 成长任务列表 -->
        <section class="data-panel">
          <PanelHeader title="成长任务" description="实时拉取 WorkBuddy 成长计划任务状态。" />
          <div v-if="!growth.data.value.tasks?.length" class="compact-empty">暂无成长任务。</div>
          <div v-else class="growth-task-list">
            <div v-for="task in activeTasks" :key="task.task_code" class="growth-task" :class="`growth-task--${taskStatus(task)}`">
              <div class="growth-task-icon"><component :is="task.locked ? Globe : Sparkles" :size="16" /></div>
              <div class="growth-task-body">
                <strong>{{ task.title ?? task.task_code }}<span v-if="task.is_new" class="badge-new">NEW</span></strong>
                <small v-if="task.task_desc">{{ task.task_desc }}</small>
                <span class="growth-task-meta">
                  <StatePill :value="taskStatus(task)" />
                  <span class="task-label">{{ taskStatusLabel(taskStatus(task)) }}</span>
                  <template v-if="typeof task.progress_current === 'number' && typeof task.progress_target === 'number'">· 进度 {{ task.progress_current }}/{{ task.progress_target }}</template>
                  <template v-if="task.reward_credit">· 奖励 {{ task.reward_credit }} 积分</template>
                  <template v-if="task.reward_energy">· {{ task.reward_energy }} 能量</template>
                  <template v-if="task.tag">· {{ task.tag }}</template>
                </span>
              </div>
            </div>
            <button v-if="completedTasks.length" class="growth-task-collapse" type="button" @click="showCompleted = !showCompleted">
              <ChevronDown v-if="!showCompleted" :size="14" /><ChevronUp v-else :size="14" />
              {{ showCompleted ? '收起' : '展开' }}已完成任务 ({{ completedTasks.length }})
            </button>
            <template v-if="showCompleted">
              <div v-for="task in completedTasks" :key="task.task_code" class="growth-task growth-task--completed">
                <div class="growth-task-icon"><CheckCircle :size="16" /></div>
                <div class="growth-task-body">
                  <strong>{{ task.title ?? task.task_code }}</strong>
                  <span class="growth-task-meta">
                    <StatePill value="completed" />
                    <span class="task-label">已完成</span>
                    <template v-if="task.reward_credit">· 奖励 {{ task.reward_credit }} 积分</template>
                  </span>
                </div>
              </div>
            </template>
          </div>
        </section>

        <!-- 连登热力图 -->
        <section class="data-panel">
          <PanelHeader title="连登地图" description="活跃热力图 · 连续登录天数 · 抽奖机会。" />
          <div class="streak-toolbar">
            <div class="streak-stats">
              <span class="streak-days">连登 {{ growth.data.value.streak?.days ?? "--" }} 天</span>
              <template v-if="growth.data.value.streak?.next_tier"> · 距 {{ growth.data.value.streak.next_tier }} 还差 {{ growth.data.value.streak.next_tier_remaining ?? '--' }} 天</template>
              · 补登卡 {{ growth.data.value.streak?.makeup_balance ?? 0 }}/{{ growth.data.value.streak?.makeup_max ?? 4 }}
            </div>
            <div v-if="(growth.data.value.lottery?.available_chances ?? 0) > 0" class="streak-lottery">🎲 可抽奖 {{ growth.data.value.lottery.available_chances }} 次</div>
          </div>
          <div v-if="growth.data.value.heatmap?.today" class="streak-today" :class="{ active: growth.data.value.heatmap.today.is_active }">{{ growth.data.value.heatmap.today.status_text ?? (growth.data.value.heatmap.today.is_active ? '今日已活跃' : '今日未活跃') }}</div>
          <div v-if="heatmapGrid.weeks.length" class="heatmap-grid">
            <div v-for="(week, wi) in heatmapGrid.weeks" :key="wi" class="heatmap-week">
              <div v-for="(cell, ci) in week" :key="ci" class="heatmap-cell" :class="`heatmap-cell--lvl${cellLevel(cell)}`" :title="cellTitle(cell)"></div>
            </div>
          </div>
          <div class="heatmap-legend">
            <span class="heatmap-legend-label">少</span>
            <div class="heatmap-cell heatmap-cell--lvl0"></div>
            <div class="heatmap-cell heatmap-cell--lvl1"></div>
            <div class="heatmap-cell heatmap-cell--lvl2"></div>
            <div class="heatmap-cell heatmap-cell--lvl3"></div>
            <div class="heatmap-cell heatmap-cell--lvl4"></div>
            <span class="heatmap-legend-label">多</span>
          </div>
        </section>

        <!-- 自动化执行历史 -->
        <section class="data-panel">
          <PanelHeader title="执行历史" :description="`共 ${historyTotal} 条 · 第 ${historyPage} / ${Math.max(1, historyPages)} 页 · 北京时间`" />
          <div v-if="history.isPending.value" class="loading-row">正在读取执行历史…</div>
          <div v-else-if="!history.data.value?.logs?.length" class="compact-empty">暂无执行记录。</div>
          <div v-else class="table-wrap">
            <table class="data-table growth-history-table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>触发</th>
                  <th v-for="s in stepConfigs" :key="s.key" :title="s.label">{{ s.label }}</th>
                  <th>详情</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="entry in history.data.value.logs" :key="entry.id">
                  <td><span class="mono">{{ formatBeijing(entry.created_at) }}</span></td>
                  <td><span class="history-trigger">{{ triggerLabel(entry.triggered_by) }}</span></td>
                  <td v-for="s in stepConfigs" :key="s.key">
                    <span v-if="entry.results[s.key]" class="hstep" :class="`hstep--${stepTone(entry.results[s.key]!.status)}`" :title="stepDetail(entry.results[s.key]!)">{{ stepShort(entry.results[s.key]!.status) }}</span>
                    <span v-else class="hstep hstep--none">—</span>
                  </td>
                  <td>
                    <details class="hist-details">
                      <summary>详情</summary>
                      <div class="hist-detail-lines">
                        <div v-for="(res, key) in entry.results" :key="String(key)" class="hist-detail-line">{{ String(key) }}: {{ stepDetail(res) }}</div>
                      </div>
                    </details>
                  </td>
                </tr>
              </tbody>
            </table>
            <div class="history-pager">
              <button class="secondary-button compact-button" type="button" :disabled="historyPage <= 1 || history.isFetching.value" @click="setHistoryPage(-1)">上一页</button>
              <span>第 {{ historyPage }} / {{ Math.max(1, historyPages) }} 页 · 共 {{ historyTotal }} 条</span>
              <button class="secondary-button compact-button" type="button" :disabled="historyPage >= historyPages || history.isFetching.value" @click="setHistoryPage(1)">下一页</button>
            </div>
          </div>
        </section>
      </template>
      <div v-else class="loading-row">正在读取成长中心数据…</div>
    </template>

    <NotificationRegion :notifications="notifications" @dismiss="dismiss" />
  </section>
</template>

<style scoped>
.growth-page { gap: 14px; }
.automation-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1px; background: var(--line); border-top: 1px solid var(--line); }
.automation-card { display: flex; flex-direction: column; gap: 0; padding: 0; background: var(--surface); }
.automation-card--off { opacity: 0.7; }
.automation-card-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 10px 14px; border-bottom: 1px solid var(--line); }
.automation-card-title { display: flex; align-items: center; gap: 7px; color: var(--accent); }
.automation-card-title strong { color: var(--text); font-size: var(--text-sm); font-weight: 600; }
.automation-card-body { display: flex; flex-direction: column; gap: 5px; padding: 10px 14px; flex: 1; }
.automation-result { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.automation-result--empty { opacity: 0.5; }
.automation-detail { color: var(--muted); font-size: var(--text-xs); line-height: 1.4; }
.automation-meta { margin-top: auto; }
.automation-meta small { color: var(--faint); font-size: 10px; }
.active-day-today { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 6px; padding-top: 8px; border-top: 1px dashed var(--line); }
.active-day-chip { color: var(--muted); font-size: 11px; }
.automation-card-footer { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 8px 14px; border-top: 1px solid var(--line); }
.growth-task-list { display: grid; gap: 0; }
.growth-task-collapse { display: flex; align-items: center; justify-content: center; gap: 5px; padding: 8px 14px; border: 0; border-top: 1px solid var(--line); background: var(--surface-raised); color: var(--muted); font-size: 11px; }
.growth-task-collapse:hover { color: var(--accent); }
.growth-task { display: grid; grid-template-columns: 32px minmax(0, 1fr); gap: 10px; align-items: start; min-height: 56px; padding: 10px 14px; border-bottom: 1px solid var(--line); }
.growth-task:last-child { border-bottom: 0; }
.growth-task-icon { display: grid; place-items: center; width: 32px; height: 32px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface-muted); color: var(--accent); }
.growth-task-body { display: grid; gap: 3px; min-width: 0; }
.growth-task-body strong { font-size: var(--text-sm); color: var(--text); display: flex; align-items: center; gap: 6px; }
.growth-task-body small { color: var(--muted); font-size: 11px; line-height: 1.5; }
.growth-task-meta { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; margin-top: 2px; color: var(--faint); font-size: 11px; }
.task-label { color: var(--muted); }
.badge-new { padding: 1px 5px; border: 1px solid var(--accent-line); border-radius: var(--radius); background: var(--accent-soft); color: var(--accent); font-size: 9px; font-weight: 700; letter-spacing: 0.05em; }
.growth-task--completed { opacity: 0.65; }
.growth-task--claimable { border-left: 2px solid var(--accent); }
.growth-task--locked { opacity: 0.42; }
.growth-task--not_accepted { border-left: 2px solid var(--warn); }
.streak-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 14px; border-bottom: 1px solid var(--line); }
.streak-stats { color: var(--muted); font-size: 12px; }
.streak-days { color: var(--accent); font-weight: 600; font-size: 14px; font-variant-numeric: tabular-nums; }
.streak-lottery { color: var(--ok); font-size: 12px; font-weight: 600; }
.streak-today { margin: 8px 14px 4px; padding: 6px 10px; border-radius: var(--radius); background: var(--surface-muted); color: var(--faint); font-size: 11px; }
.streak-today.active { background: var(--ok-soft); color: var(--ok); border: 1px solid var(--ok-line); }
.heatmap-grid { display: flex; gap: 3px; padding: 8px 14px 4px; overflow-x: auto; }
.heatmap-week { display: grid; grid-template-rows: repeat(7, 11px); gap: 3px; }
.heatmap-cell { width: 11px; height: 11px; border-radius: var(--radius); background: var(--surface-muted); border: 1px solid var(--line); }
.heatmap-cell--lvl1 { background: rgb(232 145 58 / 0.25); border-color: transparent; }
.heatmap-cell--lvl2 { background: rgb(232 145 58 / 0.45); border-color: transparent; }
.heatmap-cell--lvl3 { background: rgb(232 145 58 / 0.7); border-color: transparent; }
.heatmap-cell--lvl4 { background: var(--accent); border-color: transparent; }
.heatmap-legend { display: flex; align-items: center; gap: 4px; padding: 4px 14px 8px; }
.heatmap-legend-label { color: var(--faint); font-size: 10px; }
.heatmap-legend .heatmap-cell { width: 10px; height: 10px; }
.history-list { display: grid; gap: 0; }
.growth-history-table th, .growth-history-table td { padding: 8px 10px; white-space: nowrap; }
.growth-history-table td:last-child { white-space: normal; max-width: 360px; }
.hstep { display: inline-flex; align-items: center; gap: 4px; padding: 1px 6px; border-radius: var(--radius); font-size: 11px; line-height: 1.5; white-space: nowrap; }
.hstep::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.hstep--ok { color: var(--ok); }
.hstep--bad { color: var(--err); }
.hstep--run { color: var(--accent); }
.hstep--skip { color: var(--muted); }
.hstep--none { opacity: 0.35; }
.hstep--none::before { display: none; }
.history-pager { display: flex; align-items: center; justify-content: flex-end; gap: 12px; padding: 10px 14px; border-top: 1px solid var(--line); color: var(--muted); font-size: 12px; }
.hist-details summary { cursor: pointer; color: var(--accent); font-size: 11px; }
.hist-detail-lines { display: grid; gap: 2px; margin-top: 4px; color: var(--muted); font-size: 11px; white-space: pre-wrap; }
.hist-detail-line { padding-left: 2px; }
.history-entry { display: grid; grid-template-columns: 200px minmax(0, 1fr); gap: 12px; padding: 10px 14px; border-bottom: 1px solid var(--line); }
.history-entry:last-child { border-bottom: 0; }
.history-time { display: flex; flex-direction: column; gap: 2px; }
.history-time small { color: var(--faint); font-size: 11px; font-variant-numeric: tabular-nums; }
.history-trigger { color: var(--accent); font-size: 10px; font-weight: 600; }
.history-summary { color: var(--muted); font-size: 11px; line-height: 1.6; }
@media (max-width: 760px) { .automation-grid { grid-template-columns: 1fr; } .history-entry { grid-template-columns: 1fr; gap: 4px; } }
</style>
