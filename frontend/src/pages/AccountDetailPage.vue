<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { Activity, ArrowLeft, BadgeCheck, KeyRound, RefreshCcw, ShieldCheck, Trash2, RotateCcw } from "@lucide/vue";
import { computed, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { apiRequest } from "@/api/client";
import ConfirmDialog from "@/components/ConfirmDialog.vue";
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
function metricValue(metric: Metric): string { return metric.value ? JSON.stringify(metric.value) : "尚无可用数据"; }
function setPurpose(name: string, event: Event): void { if (name === "chat") draftChat.value = (event.target as HTMLInputElement).checked; else draftCheckin.value = (event.target as HTMLInputElement).checked; }

async function accountCheckinHistory(selectedProvider: string, selectedAccountId: string): Promise<CheckinAttempt[]> {
  const runs = await apiRequest<{ runs: { run_id: string }[] }>("/checkin/runs?limit=20");
  const details = await Promise.all(runs.runs.map(async ({ run_id }) => apiRequest<{ attempts: CheckinAttempt[] }>(`/checkin/runs/${encodeURIComponent(run_id)}`)));
  return details.flatMap((item) => item.attempts).filter((item) => item.provider === selectedProvider && item.account_id === selectedAccountId);
}
</script>

<template>
  <section class="page-content">
    <header class="page-header"><div><h1>{{ account.data.value?.label ?? accountId }}</h1><p>{{ provider }} / {{ accountId }} · 账号、凭据、指标与签到活动均按用途隔离。</p></div><div class="header-actions"><button class="secondary-button" type="button" @click="router.push({ name: 'accounts' })"><ArrowLeft :size="16" />账号池</button><button class="secondary-button" type="button" :disabled="account.isFetching.value" @click="account.refetch()"><RefreshCcw :class="{ spin: account.isFetching.value }" :size="16" />刷新视图</button></div></header>
    <div v-if="account.isError.value" class="data-state data-state--error">账号读取失败：{{ account.error.value }}</div>
    <template v-else-if="account.data.value">
      <div v-if="isEnv" class="security-banner"><ShieldCheck :size="18" /><div><strong>环境变量账号</strong><span>此账号仅兼容既有代理请求配置。请先提升为持久账号，才能编辑凭据或关联签到用途。</span></div></div>
      <section class="overview-grid">
        <div class="data-panel"><PanelHeader title="账号概览" description="只显示安全的身份掩码与用途状态。" /><dl class="detail-list"><div><dt>来源</dt><dd>{{ statusLabel(account.data.value.source) }}</dd></div><div><dt>身份</dt><dd>{{ account.data.value.masked_identity ?? "--" }}</dd></div><div><dt>总体状态</dt><dd><StatePill :value="account.data.value.summary_status" /></dd></div><div><dt>最近更新</dt><dd>{{ account.data.value.updated_at ?? "--" }}</dd></div></dl></div><div class="data-panel">
          <PanelHeader title="账户操作" description="危险操作会要求确认。" /><div class="form-actions">
            <button type="button" :disabled="action.isPending.value" @click="requestAction('refresh')"><RefreshCcw :size="16" />刷新</button><button class="secondary-button" type="button" :disabled="action.isPending.value" @click="requestAction('probe')"><Activity :size="16" />探测</button><button class="secondary-button" type="button" :disabled="action.isPending.value" @click="requestAction('verify')"><BadgeCheck :size="16" />验证签到</button>
            <button v-if="provider === 'qoder' && account.data.value?.purposes.checkin?.status === 'needs_import'" class="secondary-button" type="button" :disabled="action.isPending.value" @click="action.mutate('rederive')"><RotateCcw :size="16" />重新派生签到</button><button v-if="isEnv" class="secondary-button" type="button" :disabled="action.isPending.value" @click="requestAction('promote')">提升为持久账号</button><button v-else class="secondary-button" type="button" :disabled="action.isPending.value" @click="reauthorize"><KeyRound :size="16" />重新授权</button><button v-if="!isEnv" class="danger-button" type="button" :disabled="action.isPending.value" @click="requestAction('delete')"><Trash2 :size="16" />删除账号</button>
          </div>
        </div>
      </section>
      <section class="data-panel"><PanelHeader title="用途与路由状态" description="代理请求与每日签到可以独立启停，签到失败不会移出代理账号池。" /><div class="form-grid"><label>显示名称<input v-model="draftLabel" :disabled="!canWrite" aria-label="账号显示名称" /></label><label class="inline-check"><input v-model="draftEnabled" type="checkbox" :disabled="!canWrite" />账号启用</label><label v-for="name in ['chat', 'checkin']" :key="name" class="inline-check"><input :checked="name === 'chat' ? draftChat : draftCheckin" type="checkbox" :disabled="!canWrite || !account.data.value.purposes[name]" @change="setPurpose(name, $event)" /><span>{{ statusLabel(name) }}</span><StatePill v-if="account.data.value.purposes[name]" :value="account.data.value.purposes[name].status" /></label></div><div class="purpose-cards"><div v-for="(item, name) in account.data.value.purposes" :key="name"><strong>{{ statusLabel(String(name)) }}</strong><StatePill :value="item.verification_status" /><small>到期：{{ item.expires_at ?? "未设置" }} · 验证：{{ item.verified_at ?? "尚未验证" }}<template v-if="item.last_error"> · {{ item.last_error }}</template></small></div></div><div class="form-actions"><button type="button" :disabled="!canWrite" @click="requestAction('save')"><ShieldCheck :size="16" />保存账号设置</button></div></section>
      <section class="overview-grid"><div class="data-panel"><PanelHeader title="凭据元数据" description="模式、版本与过期时间；不显示原始凭据或凭据指纹。" /><div v-if="credentials.isPending.value" class="loading-row">正在读取凭据元数据…</div><div v-else-if="!credentials.data.value?.length" class="compact-empty">尚未保存持久凭据。</div><div v-else class="metric-list"><div v-for="item in credentials.data.value" :key="item.purpose"><strong>{{ statusLabel(item.purpose) }} · {{ item.mode }}</strong><StatePill :value="item.has_refresh_token ? 'refresh' : 'static'" /><span>版本 v{{ item.credential_version }} · 到期 {{ item.expires_at ?? "未设置" }}</span><small>最后更新 {{ item.updated_at }}</small></div></div></div><div class="data-panel"><PanelHeader title="积分与配额" description="未知或过期值不会显示为 0。" /><div v-if="metrics.isPending.value" class="loading-row">正在读取账号指标…</div><div v-else-if="!metrics.data.value?.snapshots.length" class="compact-empty">尚未采集账号指标。</div><div v-else class="metric-list"><div v-for="metric in metrics.data.value.snapshots" :key="metric.metric_kind"><strong>{{ metric.metric_kind }}</strong><StatePill :value="metric.status" /><span>{{ metricValue(metric) }}</span><small>{{ metric.observed_at ?? "--" }}</small></div></div></div></section>
      <section class="overview-grid"><div class="data-panel"><PanelHeader title="最近请求事件" description="仅显示脱敏元数据。" /><div v-if="events.isPending.value" class="loading-row">正在读取请求事件…</div><div v-else-if="!events.data.value?.events.length" class="compact-empty">尚无此账号的请求事件。</div><div v-else class="metric-list"><div v-for="event in events.data.value.events" :key="event.event_id"><strong>{{ event.model_id ?? "未知模型" }}</strong><StatePill :value="event.status" /><span>{{ event.latency_ms ?? "--" }} ms<template v-if="event.error_code"> · {{ event.error_code }}</template></span><small>{{ event.started_at ?? "--" }}</small></div></div></div><div class="data-panel"><PanelHeader title="签到历史" description="最近 20 个签到批次中与此账号匹配的结果。" /><div v-if="checkin.isPending.value" class="loading-row">正在读取签到历史…</div><div v-else-if="!checkin.data.value?.length" class="compact-empty">尚未记录签到结果。</div><div v-else class="metric-list"><div v-for="(item, index) in checkin.data.value" :key="`${item.finished_at}:${index}`"><strong>每日签到</strong><StatePill :value="item.outcome" /><span>{{ checkinErrorHint(item.error_code) ?? "已完成" }}</span><small>{{ item.finished_at ?? "--" }}</small></div></div></div></section>
    </template>
    <OperationStatus :operation="lastOperation" />
    <ConfirmDialog :open="Boolean(pending)" :title="pending === 'delete' ? '删除这个账号？' : pending === 'verify' ? '验证并启用签到？' : '停用这个账号？'" :description="pending === 'delete' ? '账号的持久凭据和用途记录将被删除，操作不可撤销。' : pending === 'verify' ? '系统将使用当前账号的签到凭据或已登录 Chat 凭据发送一次每日签到请求；未签到时可能立即领取当天积分。' : '停用后该账号不会参与新的代理或签到调度。'" :confirm-label="pending === 'delete' ? '确认删除' : pending === 'verify' ? '确认并验证' : '确认停用'" :tone="pending === 'delete' ? 'danger' : 'default'" :verification-text="pending === 'delete' ? 'DELETE' : ''" :busy="action.isPending.value" @cancel="pending = null" @confirm="confirmPending" />
    <NotificationRegion :notifications="notifications" @dismiss="dismiss" />
  </section>
</template>
