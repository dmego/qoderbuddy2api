<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { CalendarCheck, Eye, Play, RefreshCcw, TimerReset } from "@lucide/vue";
import { computed, reactive, ref } from "vue";

import PanelHeader from "@/components/PanelHeader.vue";
import StatePill from "@/components/StatePill.vue";
import { apiRequest } from "@/api/client";

type Account = { provider: string; account_id: string; status: string; verification_status: string };
type CheckinTarget = Pick<Account, "provider" | "account_id">;
type DailyState = { provider: string; account_id: string; terminal_outcome?: string };
type CheckinStatus = { enabled: boolean; running: boolean; local_date: string; timezone: string; checkin_at: string; next_run_at?: string; eligible_accounts: Account[]; daily_states: DailyState[] };
type CheckinRun = { run_id: string; started_at: string; finished_at?: string; status: string; trigger: string; attempt_count: number; successful_count: number };
type CheckinAttempt = { provider: string; account_id: string; outcome?: string; http_status?: number; attempts: number; finished_at?: string };
type RunDetail = { run: CheckinRun; attempts: CheckinAttempt[] };

const queryClient = useQueryClient();
const selected = reactive<Record<string, boolean>>({});
const message = ref("");
const selectedRunId = ref<string | null>(null);
const status = useQuery({ queryKey: ["checkin-status"], queryFn: () => apiRequest<CheckinStatus>("/checkin/status"), refetchInterval: 10000 });
const history = useQuery({ queryKey: ["checkin-runs"], queryFn: () => apiRequest<{ runs: CheckinRun[] }>("/checkin/runs?limit=20") });
const runDetail = useQuery({ queryKey: ["checkin-run", selectedRunId], enabled: computed(() => selectedRunId.value !== null), queryFn: () => apiRequest<RunDetail>(`/checkin/runs/${selectedRunId.value}`) });
const run = useMutation({
  mutationFn: () => apiRequest<{ run_id: string }>("/checkin/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(targetsBody()) }),
  onSuccess: async (result) => {
    message.value = `批次 ${result.run_id} 已完成`;
    await queryClient.invalidateQueries({ queryKey: ["checkin-status"] });
    await queryClient.invalidateQueries({ queryKey: ["checkin-runs"] });
  },
});

function targetsBody(): { targets?: CheckinTarget[] } {
  const targets = Object.entries(selected)
    .filter(([, enabled]) => enabled)
    .map(([key]) => { const [provider, account_id] = key.split(":"); return { provider, account_id }; });
  return targets.length ? { targets } : {};
}

function toggle(key: string): void { selected[key] = !selected[key]; }
function inspectRun(runId: string): void { selectedRunId.value = runId; }
function dailyOutcome(account: Account): string { return (status.data.value?.daily_states ?? []).find((item) => item.provider === account.provider && item.account_id === account.account_id)?.terminal_outcome ?? "待执行"; }
</script>

<template>
  <section class="page-content">
    <header class="page-header"><div><p class="eyebrow">Daily operations</p><h1>签到中心</h1><p>按账号执行 CodeBuddy / WorkBuddy 与 Qoder 签到，10001 已签到响应按业务成功处理。</p></div><div class="header-actions"><button class="secondary-button" type="button" @click="status.refetch()"><RefreshCcw :size="16" />刷新</button><button type="button" :disabled="run.isPending.value || status.data.value?.running" @click="run.mutate()"><Play :size="16" />执行选中账号</button></div></header>
    <div class="checkin-rail"><div><span>今日日期</span><strong>{{ status.data.value?.local_date ?? "--" }}</strong></div><div><span>调度时间</span><strong>{{ status.data.value?.checkin_at ?? "--" }} · {{ status.data.value?.timezone ?? "--" }}</strong></div><div><span>下一次</span><strong>{{ status.data.value?.next_run_at ?? "未启用" }}</strong></div><div><span>运行状态</span><StatePill :value="status.data.value?.running ? 'running' : status.data.value?.enabled ? 'enabled' : 'disabled'" /></div></div>
    <section class="data-panel"><PanelHeader title="可签到账号" description="只展示 checkin purpose 已验证并启用的账号"><template #default><TimerReset :size="17" /></template></PanelHeader><div v-if="!(status.data.value?.eligible_accounts?.length)" class="empty-state"><CalendarCheck :size="28" /><strong>暂无可签到账号</strong><span>请先导入签到凭据并完成验证。</span></div><div v-else class="table-wrap"><table><thead><tr><th>执行</th><th>账号</th><th>Provider</th><th>验证</th><th>今日状态</th></tr></thead><tbody><tr v-for="account in status.data.value?.eligible_accounts ?? []" :key="`${account.provider}:${account.account_id}`"><td><input :checked="selected[`${account.provider}:${account.account_id}`]" type="checkbox" @change="toggle(`${account.provider}:${account.account_id}`)" /></td><td class="mono">{{ account.account_id }}</td><td>{{ account.provider }}</td><td><StatePill :value="account.verification_status" /></td><td><StatePill :value="dailyOutcome(account)" /></td></tr></tbody></table></div></section>
    <section class="data-panel"><PanelHeader title="最近批次" description="持久化记录，Control Plane 重启后仍可查看"><template #default><button class="icon-button" type="button" title="刷新签到历史" @click="history.refetch()"><RefreshCcw :size="15" /></button></template></PanelHeader><div v-if="!(history.data.value?.runs?.length)" class="compact-empty">尚未执行签到批次</div><div v-else class="table-wrap"><table><thead><tr><th>开始时间</th><th>触发方式</th><th>结果</th><th>状态</th><th></th></tr></thead><tbody><tr v-for="item in history.data.value?.runs ?? []" :key="item.run_id"><td><span class="mono">{{ item.run_id }}</span><small>{{ item.started_at }}</small></td><td>{{ item.trigger }}</td><td>{{ item.successful_count }} / {{ item.attempt_count }}</td><td><StatePill :value="item.status" /></td><td><button class="icon-button" type="button" title="查看签到尝试" @click="inspectRun(item.run_id)"><Eye :size="16" /></button></td></tr></tbody></table></div><div v-if="selectedRunId" class="attempt-section"><PanelHeader title="批次明细" :description="selectedRunId" /><div v-if="runDetail.isPending.value" class="loading-row">正在读取已脱敏的尝试记录…</div><div v-else-if="runDetail.isError.value" class="form-error">无法读取批次明细</div><div v-else-if="!runDetail.data.value?.attempts?.length" class="compact-empty">该批次没有账号尝试记录</div><div v-else class="table-wrap"><table><thead><tr><th>账号</th><th>结果</th><th>HTTP</th><th>重试次数</th><th>完成时间</th></tr></thead><tbody><tr v-for="attempt in runDetail.data.value?.attempts ?? []" :key="`${attempt.provider}:${attempt.account_id}`"><td>{{ attempt.provider }}<small>{{ attempt.account_id }}</small></td><td><StatePill :value="attempt.outcome" /></td><td>{{ attempt.http_status ?? "--" }}</td><td>{{ attempt.attempts }}</td><td>{{ attempt.finished_at ?? "--" }}</td></tr></tbody></table></div></div></section>
    <p v-if="message" class="form-message">{{ message }}</p><p v-if="run.isError.value" class="form-error">{{ run.error.value }}</p>
  </section>
</template>
