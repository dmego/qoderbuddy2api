<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { Activity, ArrowLeft, BadgeCheck, KeyRound, RefreshCcw, ShieldCheck, Trash2, RotateCcw } from "@lucide/vue";
import { computed, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { apiRequest } from "@/api/client";
import ConfirmDialog from "@/components/ConfirmDialog.vue";
import MetricChart from "@/components/MetricChart.vue";
import NotificationRegion from "@/components/NotificationRegion.vue";
import OperationStatus from "@/components/OperationStatus.vue";
import PanelHeader from "@/components/PanelHeader.vue";
import StatePill from "@/components/StatePill.vue";
import { useNotifications } from "@/composables/useNotifications";
import { statusLabel } from "@/utils/presentation";

type Purpose = { enabled: boolean; status: string; verification_status: string; expires_at?: string; verified_at?: string; last_error?: string };
type Account = { provider: string; account_id: string; label: string; source: string; enabled: boolean; summary_status: string; masked_identity?: string; created_at?: string; updated_at?: string; purposes: Record<string, Purpose> };
type Credential = { provider: string; account_id: string; purpose: string; mode: string; credential_version: number; expires_at?: string; has_refresh_token: boolean; updated_at: string };
type Metric = { metric_kind: string; status: string; observed_at?: string; value: Record<string, unknown> | null };
type MetricHistoryRow = { observed_at: string; status: string; value: Record<string, unknown> | null };
type RequestEvent = { event_id: string; model_id?: string; status: string; latency_ms?: number; started_at?: string; error_code?: string };
type CheckinAttempt = { provider: string; account_id: string; outcome: string; finished_at?: string; error_code?: string };
type DetailAction = "save" | "refresh" | "probe" | "verify" | "rederive" | "promote" | "delete";

const route = useRoute();
const router = useRouter();
const queryClient = useQueryClient();
const provider = computed(() => String(route.params.provider));
const accountId = computed(() => String(route.params.accountId));
const base = computed(() => `/accounts/${encodeURIComponent(provider.value)}/${encodeURIComponent(accountId.value)}`);
const draftLabel = ref("");
const draftEnabled = ref(true);
const draftChat = ref(false);
const draftCheckin = ref(false);
const pending = ref<DetailAction | null>(null);
const lastOperation = ref<Record<string, unknown> | null>(null);
const { notifications, notify, dismiss } = useNotifications();

const account = useQuery({ queryKey: ["account-detail", provider, accountId], queryFn: () => apiRequest<Account>(base.value), staleTime: 15_000 });
const credentials = useQuery({ queryKey: ["account-credentials", provider, accountId], queryFn: async () => {
  const result = await apiRequest<{ credentials: Credential[] }>(`/credentials?provider=${encodeURIComponent(provider.value)}`);
  return result.credentials.filter((item) => item.account_id === accountId.value);
} });
const metrics = useQuery({ queryKey: ["account-metrics", provider, accountId], queryFn: () => apiRequest<{ snapshots: Metric[] }>(`/metrics/accounts/${encodeURIComponent(provider.value)}/${encodeURIComponent(accountId.value)}`) });
const pointsHistory = useQuery({ queryKey: ["account-metric-history", provider, accountId], queryFn: () => apiRequest<{ rows: MetricHistoryRow[] }>(`/metrics/accounts/${encodeURIComponent(provider.value)}/${encodeURIComponent(accountId.value)}/history/points?limit=500`), staleTime: 30_000 });
const events = useQuery({ queryKey: ["account-events", provider, accountId], queryFn: () => apiRequest<{ events: RequestEvent[] }>(`/usage/events?limit=10&provider=${encodeURIComponent(provider.value)}&account_id=${encodeURIComponent(accountId.value)}`) });
const checkin = useQuery({ queryKey: ["account-checkin", provider, accountId], queryFn: () => accountCheckinHistory(provider.value, accountId.value) });

watch(() => account.data.value, (value) => {
  if (!value) return;
  draftLabel.value = value.label;
  draftEnabled.value = value.enabled;
  draftChat.value = value.purposes.chat?.enabled ?? false;
  draftCheckin.value = value.purposes.checkin?.enabled ?? false;
}, { immediate: true });

const action = useMutation({
  mutationFn: (kind: DetailAction) => executeAction(kind),
  onSuccess: async (result, kind) => {
    const data = result as Record<string, unknown>;
    if (kind === "verify") {
      const items = Array.isArray(data.results) ? (data.results as Record<string, unknown>[]).map((r) => ({
        key: `${r.provider}:${r.account_id}`,
        label: `${r.provider} / ${r.account_id}`,
        status: String(r.outcome ?? "pending"),
        error: r.business_code ? `业务码 ${r.business_code}` : (r.message ? String(r.message) : ""),
      })) : undefined;
      lastOperation.value = { action: actionLabel(kind), status: "succeeded", ...data, items };
      notify(`${actionLabel(kind)}已完成`, { tone: items?.length ? (items.some((i) => i.status === "CLAIMED") ? "success" : "info") : "success" });
    } else {
      lastOperation.value = { action: actionLabel(kind), status: "succeeded", ...data };
      notify(`${actionLabel(kind)}已完成`, { tone: "success" });
    }
    if (kind === "delete") { await router.replace({ name: "accounts" }); return; }
    const promoted = (result as { account?: Account }).account;
    if (kind === "promote" && promoted) await router.replace({ name: "account-detail", params: { provider: promoted.provider, accountId: promoted.account_id } });
    await Promise.all([account.refetch(), credentials.refetch(), metrics.refetch(), events.refetch(), checkin.refetch(), queryClient.invalidateQueries({ queryKey: ["accounts"] })]);
  },
  onError: (error) => notify("账号操作失败", { message: String(error), tone: "error", timeout: 0 }),
});

const isEnv = computed(() => account.data.value?.source === "env");
const canWrite = computed(() => !isEnv.value && !action.isPending.value);

function executeAction(kind: DetailAction): Promise<unknown> {
  if (kind === "save") return apiRequest(base.value, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(saveBody()) });
  if (kind === "refresh") return apiRequest(`${base.value}/refresh`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  if (kind === "probe") return apiRequest(`${base.value}/probe`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  if (kind === "verify") return apiRequest(`${base.value}/verify-checkin`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  if (kind === "rederive") return apiRequest(`${base.value}/rederive-checkin`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  if (kind === "promote") return apiRequest(`${base.value}/promote`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ label: draftLabel.value }) });
  return apiRequest(base.value, { method: "DELETE" });
}

function saveBody(): Record<string, unknown> {
  const current = account.data.value;
  const purposes: Record<string, { enabled: boolean }> = {};
  if (current?.purposes.chat) purposes.chat = { enabled: draftChat.value };
  if (current?.purposes.checkin) purposes.checkin = { enabled: draftCheckin.value };
  return { label: draftLabel.value, enabled: draftEnabled.value, purposes };
}

function requestAction(kind: DetailAction): void { if (kind === "delete" || kind === "verify" || (kind === "save" && !draftEnabled.value)) pending.value = kind; else action.mutate(kind); }
function confirmPending(): void { if (!pending.value) return; const kind = pending.value; pending.value = null; action.mutate(kind); }
function reauthorize(): void { void router.push({ name: "account-add", query: { provider: provider.value, accountId: accountId.value, label: draftLabel.value } }); }
function actionLabel(kind: DetailAction): string { return ({ save: "保存账号", refresh: "刷新账号", probe: "探测账号", verify: "验证签到", rederive: "重新派生签到", promote: "提升环境账号", delete: "删除账号" } as Record<DetailAction, string>)[kind]; }
function checkinErrorHint(code: string | null | undefined): string | null {
  if (code === "10001") return null;
  if (code === "checkin_failed") return null;
  return code ?? null;
}
function metricValue(metric: Metric): string {
  const value = metric.value as { unit?: string; total_remaining?: number; total_used?: number; total_capacity?: number; activities?: { model?: string; tag?: string; limit?: number; used?: number; remaining?: number }[] } | null;
  if (metric.metric_kind === "points" && value && typeof value.total_remaining === "number") {
    return `剩余 ${value.total_remaining} ${value.unit ?? "credits"}（已用 ${value.total_used ?? 0} / 总 ${value.total_capacity ?? 0}）`;
  }
  if (metric.metric_kind === "activity" && value && Array.isArray(value.activities)) {
    if (!value.activities.length) return "当前无免费模型活动";
    return value.activities.map((a) => `${a.model ?? "?"}: 剩 ${a.remaining ?? 0}/${a.limit ?? 0}（tag ${a.tag ?? "-"}）`).join("；");
  }
  return metric.value ? JSON.stringify(metric.value) : "尚无可用数据";
}
const creditsChart = computed(() => {
  const rows = pointsHistory.data.value?.rows ?? [];
  const labels: string[] = [];
  const values: number[] = [];
  for (const row of rows) {
    const total = (row.value as { total_remaining?: number } | null)?.total_remaining;
    if (typeof total !== "number") continue;
    labels.push(row.observed_at.slice(5, 16));
    values.push(total);
  }
  return { labels, values };
});
function setPurpose(name: string, event: Event): void { if (name === "chat") draftChat.value = (event.target as HTMLInputElement).checked; else draftCheckin.value = (event.target as HTMLInputElement).checked; }

async function accountCheckinHistory(selectedProvider: string, selectedAccountId: string): Promise<CheckinAttempt[]> {
  const runs = await apiRequest<{ runs: { run_id: string }[] }>("/checkin/runs?limit=20");
  const details = await Promise.all(runs.runs.map(async ({ run_id }) => apiRequest<{ attempts: CheckinAttempt[] }>(`/checkin/runs/${encodeURIComponent(run_id)}`)));
  return details.flatMap((item) => item.attempts).filter((item) => item.provider === selectedProvider && item.account_id === selectedAccountId);
}
</script>

<template>
  <section class="page-content account-detail-page">
    <header class="page-header account-detail-header"><div><h1>{{ account.data.value?.label ?? accountId }}</h1><p class="mono">{{ provider }} / {{ accountId }}</p></div><div class="header-actions"><button class="secondary-button" type="button" @click="router.push({ name: 'accounts' })"><ArrowLeft :size="16" />账号池</button><button class="secondary-button" type="button" :disabled="account.isFetching.value" @click="account.refetch()"><RefreshCcw :class="{ spin: account.isFetching.value }" :size="16" />刷新</button></div></header>
    <div v-if="account.isError.value" class="data-state data-state--error">账号读取失败：{{ account.error.value }}</div>
    <template v-else-if="account.data.value">
      <div v-if="isEnv" class="security-banner compact"><ShieldCheck :size="18" /><strong>环境变量账号</strong><span>只读；提升后可编辑。</span></div>
      <section class="account-summary-grid"><div class="account-summary-item"><span>来源</span><strong>{{ statusLabel(account.data.value.source) }}</strong></div><div class="account-summary-item"><span>身份</span><strong class="mono">{{ account.data.value.masked_identity ?? "--" }}</strong></div><div class="account-summary-item"><span>总体状态</span><StatePill :value="account.data.value.summary_status" /></div><div class="account-summary-item"><span>最近更新</span><strong class="mono">{{ account.data.value.updated_at ?? "--" }}</strong></div></section>
      <section class="data-panel account-actions-panel"><PanelHeader title="账号操作" /><div class="form-actions"><button type="button" :disabled="action.isPending.value" @click="requestAction('refresh')"><RefreshCcw :size="16" />刷新</button><button class="secondary-button" type="button" :disabled="action.isPending.value" @click="requestAction('probe')"><Activity :size="16" />探测</button><button class="secondary-button" type="button" :disabled="action.isPending.value" @click="requestAction('verify')"><BadgeCheck :size="16" />验证签到</button><button v-if="provider === 'qoder' && !isEnv" class="secondary-button" type="button" :disabled="action.isPending.value" @click="action.mutate('rederive')"><RotateCcw :size="16" />重新派生</button><button v-if="isEnv" class="secondary-button" type="button" :disabled="action.isPending.value" @click="requestAction('promote')">提升账号</button><button v-else class="secondary-button" type="button" :disabled="action.isPending.value" @click="reauthorize"><KeyRound :size="16" />重新授权</button><button v-if="!isEnv" class="danger-button" type="button" :disabled="action.isPending.value" @click="requestAction('delete')"><Trash2 :size="16" />删除</button></div></section>
      <section class="data-panel detail-section"><PanelHeader title="用途与路由" /><div class="form-grid"><label>显示名称<input v-model="draftLabel" :disabled="!canWrite" aria-label="账号显示名称" /></label><label class="inline-check"><input v-model="draftEnabled" type="checkbox" :disabled="!canWrite" />账号启用</label><label v-for="name in ['chat', 'checkin']" :key="name" class="inline-check"><input :checked="name === 'chat' ? draftChat : draftCheckin" type="checkbox" :disabled="!canWrite || !account.data.value.purposes[name]" @change="setPurpose(name, $event)" /><span>{{ statusLabel(name) }}</span><StatePill v-if="account.data.value.purposes[name]" :value="account.data.value.purposes[name].status" /></label></div><div class="purpose-cards"><div v-for="(item, name) in account.data.value.purposes" :key="name"><strong>{{ statusLabel(String(name)) }}</strong><StatePill :value="item.verification_status" /><small>到期 {{ item.expires_at ?? "未设置" }} · 验证 {{ item.verified_at ?? "尚未验证" }}<template v-if="item.last_error"> · {{ item.last_error }}</template></small></div></div><div class="form-actions detail-save"><button type="button" :disabled="!canWrite" @click="requestAction('save')"><ShieldCheck :size="16" />保存设置</button></div></section>
      <div class="detail-main-grid"><section class="data-panel detail-section"><PanelHeader title="积分与配额" /><div v-if="metrics.isPending.value" class="loading-row fixed-empty">正在读取指标…</div><div v-else-if="!metrics.data.value?.snapshots.length" class="compact-empty fixed-empty">尚未采集指标。</div><div v-else class="metric-list"><div v-for="metric in metrics.data.value.snapshots" :key="metric.metric_kind"><strong>{{ metric.metric_kind }}</strong><StatePill :value="metric.status" /><span>{{ metricValue(metric) }}</span><small>{{ metric.observed_at ?? "--" }}</small></div></div></section><section class="data-panel detail-section"><PanelHeader title="凭据元数据" /><div v-if="credentials.isPending.value" class="loading-row fixed-empty">正在读取凭据…</div><div v-else-if="!credentials.data.value?.length" class="compact-empty fixed-empty">尚未保存凭据。</div><div v-else class="metric-list"><div v-for="item in credentials.data.value" :key="item.purpose"><strong>{{ statusLabel(item.purpose) }} · {{ item.mode }}</strong><StatePill :value="item.has_refresh_token ? 'refresh' : 'static'" /><span>版本 v{{ item.credential_version }} · 到期 {{ item.expires_at ?? "未设置" }}</span><small>{{ item.updated_at }}</small></div></div></section></div>
      <section class="data-panel detail-section trend-section"><PanelHeader title="积分趋势" /><div v-if="pointsHistory.isPending.value" class="loading-row trend-empty">正在读取趋势…</div><div v-else-if="pointsHistory.isError.value" class="data-state data-state--error trend-empty">积分历史读取失败。<button class="secondary-button compact-button" type="button" @click="pointsHistory.refetch()">重试</button></div><div v-else-if="!creditsChart.labels.length" class="compact-empty trend-empty">尚未采集积分历史。</div><MetricChart v-else :labels="creditsChart.labels" :values="creditsChart.values" /></section>
      <div class="detail-main-grid"><section class="data-panel detail-section"><PanelHeader title="最近请求" /><div v-if="events.isPending.value" class="loading-row fixed-empty">正在读取请求…</div><div v-else-if="!events.data.value?.events.length" class="compact-empty fixed-empty">尚无请求事件。</div><div v-else class="metric-list"><div v-for="event in events.data.value.events" :key="event.event_id"><strong>{{ event.model_id ?? "未知模型" }}</strong><StatePill :value="event.status" /><span>{{ event.latency_ms ?? "--" }} ms<template v-if="event.error_code"> · {{ event.error_code }}</template></span><small>{{ event.started_at ?? "--" }}</small></div></div></section><section class="data-panel detail-section"><PanelHeader title="签到历史" /><div v-if="checkin.isPending.value" class="loading-row fixed-empty">正在读取签到…</div><div v-else-if="!checkin.data.value?.length" class="compact-empty fixed-empty">尚无签到记录。</div><div v-else class="metric-list"><div v-for="(item, index) in checkin.data.value" :key="`${item.finished_at}:${index}`"><strong>每日签到</strong><StatePill :value="item.outcome" /><span>{{ checkinErrorHint(item.error_code) ?? "已完成" }}</span><small>{{ item.finished_at ?? "--" }}</small></div></div></section></div>
    </template>
    <OperationStatus :operation="lastOperation" />
    <ConfirmDialog :open="Boolean(pending)" :title="pending === 'delete' ? '删除这个账号？' : pending === 'verify' ? '验证并启用签到？' : '停用这个账号？'" :description="pending === 'delete' ? '账号的持久凭据和用途记录将被删除，操作不可撤销。' : pending === 'verify' ? '系统将使用当前账号的签到凭据或已登录 Chat 凭据发送一次每日签到请求；未签到时可能立即领取当天积分。' : '停用后该账号不会参与新的代理或签到调度。'" :confirm-label="pending === 'delete' ? '确认删除' : pending === 'verify' ? '确认并验证' : '确认停用'" :tone="pending === 'delete' ? 'danger' : 'default'" :verification-text="pending === 'delete' ? 'DELETE' : ''" :busy="action.isPending.value" @cancel="pending = null" @confirm="confirmPending" />
    <NotificationRegion :notifications="notifications" @dismiss="dismiss" />
  </section>
</template>

<style scoped>
.account-detail-page { gap: 16px; }
.account-detail-header { min-height: 72px; align-items: center; }
.account-summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border: 1px solid var(--line); background: var(--surface); }
.account-summary-item { display: grid; gap: 8px; min-height: 88px; align-content: center; padding: 16px; border-right: 1px solid var(--line); }
.account-summary-item:last-child { border-right: 0; }
.account-summary-item span { color: var(--muted); font-size: var(--text-xs); }
.account-summary-item strong { color: var(--text); font-size: var(--text-md); overflow-wrap: anywhere; }
.account-actions-panel .form-actions { min-height: 72px; padding: 0 16px 16px; }
.detail-section { overflow: hidden; }
.detail-save { justify-content: flex-end; margin-top: 16px; padding: 0 16px 16px; }
.detail-main-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(320px, 0.82fr); gap: 16px; min-width: 0; align-items: start; }
.fixed-empty { min-height: 152px; }
.trend-section { min-height: 352px; }
.trend-empty { min-height: 280px; margin: 0; }
.trend-section > .metric-chart { margin: 0; }
.detail-section .metric-list { min-height: 152px; }
.detail-main-grid > .detail-section { min-height: 210px; }
@media (max-width: 900px) {
  .account-summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .account-summary-item:nth-child(2) { border-right: 0; }
  .account-summary-item:nth-child(-n + 2) { border-bottom: 1px solid var(--line); }
  .detail-main-grid { grid-template-columns: 1fr; }
}
@media (max-width: 560px) {
  .account-summary-grid { grid-template-columns: 1fr; }
  .account-summary-item, .account-summary-item:nth-child(2) { border-right: 0; border-bottom: 1px solid var(--line); }
  .account-summary-item:last-child { border-bottom: 0; }
  .account-detail-header { min-height: 0; }
  .account-detail-header .header-actions { width: 100%; }
  .account-detail-header .header-actions > * { flex: 1; }
}
</style>
