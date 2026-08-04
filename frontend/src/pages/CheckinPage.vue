<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { Eye, Filter, Play, RefreshCcw, X } from "@lucide/vue";
import { computed, reactive, ref, watch } from "vue";

import { apiRequest } from "@/api/client";
import AccessibleDrawer from "@/components/AccessibleDrawer.vue";
import ConfirmDialog from "@/components/ConfirmDialog.vue";
import NotificationRegion from "@/components/NotificationRegion.vue";
import OperationStatus from "@/components/OperationStatus.vue";
import PaginatedTable from "@/components/PaginatedTable.vue";
import PanelHeader from "@/components/PanelHeader.vue";
import StatePill from "@/components/StatePill.vue";
import { appendQuery, useCursorPager } from "@/composables/useCursorPager";
import { useNotifications } from "@/composables/useNotifications";

type Account = { provider: string; account_id: string; label?: string; status: string; verification_status: string };
type DailyState = { provider: string; account_id: string; terminal_outcome?: string };
type SchedulerStatus = { catch_up_decision?: string; active_run_id?: string; last_error?: string; last_run_at?: string };
type MetricsStatus = { enabled: boolean; running: boolean; refresh_in_progress: boolean; last_error?: string; backoff: { metric: string; attempts: number; retry_at: string }[] };
type CheckinStatus = { enabled: boolean; running: boolean; local_date: string; timezone: string; checkin_at: string; next_run_at?: string; active_run_id?: string; scheduler?: SchedulerStatus; metrics?: MetricsStatus; eligible_accounts: Account[]; daily_states: DailyState[] };
type CheckinRun = { run_id: string; started_at: string; finished_at?: string; status: string; trigger: string; attempt_count: number; successful_count: number };
type CheckinAttempt = { provider: string; account_id: string; outcome?: string; http_status?: number; attempts: number; finished_at?: string; error_code?: string; reward_credits?: number | null; reward_expires_at?: string | null; quota_change_status?: string | null; quota_delta?: { packages?: { name?: string; delta?: number }[] } | null };
type RunPage = { runs: CheckinRun[]; next_cursor?: string | null; total?: number };

const queryClient = useQueryClient();
const selected = reactive<Record<string, boolean>>({});
const accountSearch = ref("");
const accountProvider = ref("");
const accountStatus = ref("");
const targetPage = ref(1);
const targetPageSize = 15;
const historyStatus = ref("");
const historyTrigger = ref("");
const selectedRunId = ref<string | null>(null);
const confirmRun = ref(false);
const lastOperation = ref<Record<string, unknown> | null>(null);
const { cursor, page, canPrevious, next, previous, reset } = useCursorPager();
const { notifications, notify, dismiss } = useNotifications();
const queryClientRef = queryClient;

const status = useQuery({ queryKey: ["checkin-status"], queryFn: () => apiRequest<CheckinStatus>("/checkin/status"), refetchInterval: 10000, staleTime: 15_000 });
const history = useQuery({ queryKey: ["checkin-runs", cursor, historyStatus, historyTrigger], queryFn: () => apiRequest<RunPage>(appendQuery("/checkin/runs", { limit: 20, cursor: cursor.value, status: historyStatus.value, trigger: historyTrigger.value })), staleTime: 30_000, refetchInterval: () => status.data.value?.running ? 3000 : false });
const runDetail = useQuery({ queryKey: ["checkin-run", selectedRunId], enabled: computed(() => selectedRunId.value !== null), queryFn: () => apiRequest<{ run: CheckinRun; attempts: CheckinAttempt[] }>(`/checkin/runs/${encodeURIComponent(selectedRunId.value ?? "")}`), refetchInterval: () => status.data.value?.running ? 3000 : false });
const run = useMutation({
  mutationFn: () => apiRequest<Record<string, unknown>>("/checkin/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(targetsBody()) }),
  onSuccess: async (result) => { const runId = String(result.operation_id ?? result.run_id ?? ""); lastOperation.value = { action: "手动签到", ...result }; selectedRunId.value = runId || null; notify("签到批次已启动", { message: runId || "可在最近批次中跟踪执行状态", tone: "success" }); Object.keys(selected).forEach((key) => { selected[key] = false; }); await Promise.all([queryClientRef.invalidateQueries({ queryKey: ["checkin-status"] }), queryClientRef.invalidateQueries({ queryKey: ["checkin-runs"] })]); },
  onError: (error) => notify("签到批次执行失败", { message: String(error), tone: "error", timeout: 0 }),
});

const filteredAccounts = computed(() => (status.data.value?.eligible_accounts ?? []).filter((item) => { const text = `${item.label ?? ""} ${item.account_id}`.toLowerCase(); return text.includes(accountSearch.value.trim().toLowerCase()) && (!accountProvider.value || item.provider === accountProvider.value) && (!accountStatus.value || dailyOutcome(item) === accountStatus.value); }));
const visibleAccounts = computed(() => filteredAccounts.value.slice((targetPage.value - 1) * targetPageSize, targetPage.value * targetPageSize));
const selectedTargets = computed(() => (status.data.value?.eligible_accounts ?? []).filter((item) => selected[accountKey(item)]));
const allVisibleSelected = computed(() => visibleAccounts.value.length > 0 && visibleAccounts.value.every((item) => selected[accountKey(item)]));
watch([accountSearch, accountProvider, accountStatus], () => { targetPage.value = 1; });

function targetsBody(): { targets: { provider: string; account_id: string }[] } { return { targets: selectedTargets.value.map(({ provider, account_id }) => ({ provider, account_id })) }; }
function accountKey(account: Account): string { return `${account.provider}:${account.account_id}`; }
function toggle(key: string): void { selected[key] = !selected[key]; }
function toggleVisible(): void { const nextValue = !allVisibleSelected.value; visibleAccounts.value.forEach((item) => { selected[accountKey(item)] = nextValue; }); }
function dailyOutcome(account: Account): string { return (status.data.value?.daily_states ?? []).find((item) => item.provider === account.provider && item.account_id === account.account_id)?.terminal_outcome ?? "pending"; }
function clearAccountFilters(): void { accountSearch.value = ""; accountProvider.value = ""; accountStatus.value = ""; }
function confirmExecution(): void { confirmRun.value = false; run.mutate(); }
function attemptStatus(attempt: CheckinAttempt): string {
  if (attempt.quota_change_status === "claimed_balance_increased") return "刚刚领取成功";
  if (attempt.quota_change_status === "claimed_balance_unchanged") return "已领取，余额未变化";
  if (attempt.quota_change_status === "claimed_balance_pending") return "已领取，余额待刷新";
  if (attempt.quota_change_status === "already_checked_in") return "今日已签到";
  return attempt.error_code ?? "--";
}
function attemptReward(attempt: CheckinAttempt): string {
  const reward = typeof attempt.reward_credits === "number" ? `奖励 ${attempt.reward_credits} credits` : "未返回奖励";
  if (!attempt.reward_expires_at) return reward;
  const date = new Date(attempt.reward_expires_at);
  return Number.isNaN(date.getTime()) ? reward : `${reward} · 到期 ${date.toLocaleString("zh-CN")}`;
}
function attemptDelta(attempt: CheckinAttempt): string {
  return (attempt.quota_delta?.packages ?? []).filter((item) => typeof item.delta === "number").map((item) => `${item.name ?? "配额包"} ${item.delta! >= 0 ? "+" : ""}${item.delta}`).join(" · ") || "余额差值未知";
}
</script>

<template>
  <section class="page-content">
    <header class="page-header"><div><h1>签到中心</h1><p>按已验证用途执行签到，持久化批次与脱敏尝试记录不受管理控制台重启影响。</p></div><div class="header-actions"><button class="secondary-button" type="button" :disabled="status.isFetching.value" @click="status.refetch()"><RefreshCcw :class="{ spin: status.isFetching.value }" :size="16" />刷新</button><button type="button" :disabled="run.isPending.value || status.data.value?.running || !selectedTargets.length" @click="confirmRun = true"><Play :size="16" />执行选中账号（{{ selectedTargets.length }}）</button></div></header>

    <div v-if="status.isError.value" class="data-state data-state--error" role="alert">签到状态读取失败：{{ status.error.value }}<button class="secondary-button compact-button" type="button" @click="status.refetch()">重试</button></div>
    <div v-else class="checkin-rail" :aria-busy="status.isPending.value"><div><span>今日日期</span><strong>{{ status.data.value?.local_date ?? "--" }}</strong></div><div><span>调度时间</span><strong>{{ status.data.value?.checkin_at ?? "--" }} · {{ status.data.value?.timezone ?? "--" }}</strong></div><div><span>下一次运行</span><strong>{{ status.data.value?.next_run_at ?? "未启用" }}</strong></div><div><span>补跑判定</span><strong>{{ status.data.value?.scheduler?.catch_up_decision ?? "--" }}</strong></div><div><span>活动批次</span><strong class="mono">{{ status.data.value?.scheduler?.active_run_id ?? status.data.value?.active_run_id ?? "无" }}</strong></div><div><span>运行状态</span><StatePill :value="status.data.value?.running ? 'running' : status.data.value?.enabled ? 'enabled' : 'disabled'" /></div></div>
    <div v-if="status.isStale.value && status.data.value" class="data-state data-state--warning">当前签到状态可能已过期，后台将按 10 秒间隔刷新。</div>
    <div v-if="status.data.value?.scheduler?.last_error || status.data.value?.metrics?.last_error" class="data-state data-state--warning">后台最近错误：{{ status.data.value?.scheduler?.last_error ?? status.data.value?.metrics?.last_error }}</div>
    <div v-if="status.data.value?.metrics?.backoff.length" class="data-state data-state--warning">指标退避：{{ status.data.value?.metrics?.backoff.length }} 个采集项等待重试。</div>

    <section class="data-panel filter-panel">
      <PanelHeader title="可签到账号" description="只列出每日签到用途已验证且启用的账号。"><Filter :size="17" /></PanelHeader><div class="filter-grid filter-grid--four"><label class="filter-search">账号<input v-model="accountSearch" placeholder="名称或账号 ID" /></label><label>服务提供方<select v-model="accountProvider"><option value="">全部</option><option value="codebuddy">CodeBuddy</option><option value="qoder">Qoder</option></select></label><label>今日状态<select v-model="accountStatus"><option value="">全部</option><option value="pending">待执行</option><option value="claimed">已签到</option><option value="already_checked_in">已完成</option><option value="failed">失败</option></select></label><div class="filter-actions"><button class="secondary-button" type="button" @click="clearAccountFilters"><X :size="15" />清除</button></div></div>
      <PaginatedTable aria-label="可签到账号" :loading="status.isPending.value" :empty="!visibleAccounts.length" empty-title="暂无可签到账号" empty-description="请先导入签到凭据并完成验证，或调整筛选条件。" :page="targetPage" :page-size="targetPageSize" :total="filteredAccounts.length" :can-previous="targetPage > 1" :can-next="targetPage * targetPageSize < filteredAccounts.length" @previous="targetPage -= 1" @next="targetPage += 1"><template #header><tr><th><input type="checkbox" aria-label="选择当前页全部账号" :checked="allVisibleSelected" @change="toggleVisible" /></th><th>账号</th><th>服务提供方</th><th>验证</th><th>今日状态</th></tr></template><tr v-for="account in visibleAccounts" :key="accountKey(account)"><td><input type="checkbox" :aria-label="`选择 ${account.label ?? account.account_id}`" :checked="selected[accountKey(account)]" @change="toggle(accountKey(account))" /></td><td><strong>{{ account.label ?? account.account_id }}</strong><small class="mono">{{ account.account_id }}</small></td><td><span class="provider-mark" :class="`provider-mark--${account.provider}`">{{ account.provider }}</span></td><td><StatePill :value="account.verification_status" /></td><td><StatePill :value="dailyOutcome(account)" /></td></tr></PaginatedTable>
    </section>

    <OperationStatus :operation="lastOperation" />

    <section class="data-panel"><PanelHeader title="最近批次" :description="`第 ${page} 页 · 持久化运行历史`"><div class="toolbar"><select v-model="historyTrigger" aria-label="筛选触发方式" @change="reset"><option value="">全部触发</option><option value="manual">手动</option><option value="scheduled">调度</option><option value="verify">验证</option></select><select v-model="historyStatus" aria-label="筛选批次状态" @change="reset"><option value="">全部状态</option><option value="finished">已结束</option><option value="running">运行中</option><option value="failed">失败</option></select><button class="icon-button" type="button" aria-label="刷新签到历史" title="刷新签到历史" @click="history.refetch()"><RefreshCcw :size="15" /></button></div></PanelHeader><PaginatedTable aria-label="签到运行历史" :loading="history.isPending.value" :error="history.isError.value ? `签到历史读取失败：${history.error.value}` : ''" :empty="!(history.data.value?.runs.length)" empty-title="尚未执行签到批次" empty-description="选择账号并运行后，结果会持久化到这里。" :stale="history.isStale.value" :page="page" :total="history.data.value?.total" :can-previous="canPrevious.length > 0" :can-next="Boolean(history.data.value?.next_cursor)" @retry="history.refetch()" @previous="previous" @next="next(history.data.value?.next_cursor)"><template #header><tr><th>批次 / 开始时间</th><th>触发方式</th><th>成功 / 尝试</th><th>状态</th><th>操作</th></tr></template><tr v-for="item in history.data.value?.runs ?? []" :key="item.run_id"><td><strong class="mono">{{ item.run_id }}</strong><small>{{ item.started_at }}</small></td><td>{{ item.trigger }}</td><td>{{ item.successful_count }} / {{ item.attempt_count }}</td><td><StatePill :value="item.status" /></td><td><button class="icon-button" type="button" :aria-label="`查看批次 ${item.run_id}`" :title="`查看批次 ${item.run_id}`" @click="selectedRunId = item.run_id"><Eye :size="16" /></button></td></tr></PaginatedTable></section>

    <AccessibleDrawer :open="Boolean(selectedRunId)" title="批次明细" :subtitle="selectedRunId ?? ''" close-label="关闭批次详情" @close="selectedRunId = null">
      <div v-if="runDetail.isPending.value" class="loading-row">正在读取脱敏尝试记录…</div><div v-else-if="runDetail.isError.value" class="data-state data-state--error">批次明细读取失败。<button class="secondary-button compact-button" type="button" @click="runDetail.refetch()">重试</button></div><div v-else-if="!runDetail.data.value?.attempts.length" class="compact-empty">该批次没有账号尝试记录。</div><div v-else class="table-wrap">
        <table>
          <thead><tr><th>账号</th><th>结果</th><th>奖励</th><th>余额变化</th><th>HTTP</th><th>尝试</th></tr></thead><tbody>
            <tr v-for="attempt in runDetail.data.value?.attempts ?? []" :key="`${attempt.provider}:${attempt.account_id}`">
              <td>{{ attempt.provider }}<small>{{ attempt.account_id }}</small></td><td><StatePill :value="attempt.outcome" /><small>{{ attemptStatus(attempt) }}</small></td><td>{{ attemptReward(attempt) }}</td><td>{{ attemptDelta(attempt) }}</td><td>{{ attempt.http_status ?? "--" }}</td><td>{{ attempt.attempts }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </AccessibleDrawer>

    <ConfirmDialog :open="confirmRun" title="执行签到批次？" :description="`将按顺序处理 ${selectedTargets.length} 个账号；单个账号失败不会阻止后续账号。请求会调用已配置的上游签到接口。`" confirm-label="开始执行" :busy="run.isPending.value" @cancel="confirmRun = false" @confirm="confirmExecution" />
    <NotificationRegion :notifications="notifications" @dismiss="dismiss" />
  </section>
</template>
