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

type RequestEvent = { event_id: string; model_id?: string; status: string; latency_ms?: number; started_at?: string; error_code?: string };
type CreditPackage = { name?: string; remaining?: number; total?: number; used?: number; available?: number; cap?: number; unit?: string; expires_at?: string };
type CheckinAttempt = { provider: string; account_id: string; outcome: string; http_status?: number; finished_at?: string; error_code?: string; reward_credits?: number | null; reward_expires_at?: string | null; quota_after?: { packages?: CreditPackage[] } | null; quota_delta?: { packages?: { name?: string; delta?: number }[] } | null; quota_change_status?: string | null };
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
const eventsPage = ref(1);
const checkinPage = ref(1);
const listPageSize = 10;
const { notifications, notify, dismiss } = useNotifications();
const isEnv = computed(() => account.data.value?.source === "env");

const account = useQuery({ queryKey: ["account-detail", provider, accountId], queryFn: () => apiRequest<Account>(base.value), staleTime: 15_000 });
const events = useQuery({ queryKey: ["account-events", provider, accountId], queryFn: () => apiRequest<{ events: RequestEvent[] }>(`/usage/events?limit=100&provider=${encodeURIComponent(provider.value)}&account_id=${encodeURIComponent(accountId.value)}`) });
const checkin = useQuery({ queryKey: ["account-checkin", provider, accountId], queryFn: () => accountCheckinHistory(provider.value, accountId.value) });

watch(() => account.data.value, (value) => {
  if (!value) return;
  draftLabel.value = value.label;
  draftEnabled.value = value.enabled;
  draftChat.value = value.purposes.chat?.enabled ?? false;
  draftCheckin.value = value.purposes.checkin?.enabled ?? false;
}, { immediate: true });
watch([() => events.data.value?.events?.length, () => checkin.data.value?.length], () => { eventsPage.value = 1; checkinPage.value = 1; });

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
    await Promise.all([account.refetch(), events.refetch(), checkin.refetch(), queryClient.invalidateQueries({ queryKey: ["accounts"] })]);
  },
  onError: (error) => notify("账号操作失败", { message: String(error), tone: "error", timeout: 0 }),
});

const canWrite = computed(() => !isEnv.value && !action.isPending.value);
const visibleEvents = computed(() => (events.data.value?.events ?? []).slice((eventsPage.value - 1) * listPageSize, eventsPage.value * listPageSize));
const eventPageCount = computed(() => Math.max(1, Math.ceil((events.data.value?.events.length ?? 0) / listPageSize)));
const visibleCheckins = computed(() => (checkin.data.value ?? []).slice((checkinPage.value - 1) * listPageSize, checkinPage.value * listPageSize));
const checkinPageCount = computed(() => Math.max(1, Math.ceil((checkin.data.value?.length ?? 0) / listPageSize)));

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
  if (code === "qoder_checkin_disabled") return "Qoder 今日签到活动已关闭";
  if (code === "qoder_checkin_reauth_required") return "签到凭据已失效，请重新派生或导入";
  if (code === "10001") return null;
  if (code === "checkin_failed") return null;
  return code ?? null;
}
function checkinQuotaHint(item: CheckinAttempt): string {
  if (item.quota_change_status === "claimed_balance_increased") return "刚刚领取成功";
  if (item.quota_change_status === "claimed_balance_unchanged") return "已领取，余额未变化";
  if (item.quota_change_status === "claimed_balance_pending") return "已领取，余额待刷新";
  if (item.quota_change_status === "already_checked_in") return "今日已签到";
  if (item.error_code === "qoder_checkin_disabled") return "Qoder 今日签到活动已关闭";
  if (item.error_code === "qoder_checkin_reauth_required" || item.outcome === "needs_reauth") return "签到凭据已失效，请重新派生或导入";
  if (item.outcome === "failed") {
    if (item.provider === "qoder" && item.http_status === 400) {
      return "Qoder 今日签到不可用，请重新派生凭据或等待活动恢复";
    }
    return item.http_status === 400 ? "上游拒绝请求，请重新验证" : "签到失败";
  }
  return checkinErrorHint(item.error_code) ?? "已完成";
}
function checkinReward(item: CheckinAttempt): string {
  if (item.outcome !== "claimed" && item.outcome !== "already_checked_in") return "未发放奖励";
  const reward = typeof item.reward_credits === "number" ? `奖励 ${item.reward_credits.toLocaleString()} credits` : "未返回奖励";
  const expiry = formatExpiry(item.reward_expires_at ?? undefined);
  return expiry ? `${reward} · ${expiry}` : reward;
}
function checkinDelta(item: CheckinAttempt): string | null {
  const values = item.quota_delta?.packages?.filter((pkg) => typeof pkg.delta === "number") ?? [];
  if (!values.length) return null;
  return values.map((pkg) => `${pkg.name ?? "配额包"} ${pkg.delta! >= 0 ? "+" : ""}${pkg.delta}`).join(" · ");
}
function formatExpiry(expiresAt?: string): string | null {
  if (!expiresAt) return null;
  const d = new Date(expiresAt);
  if (Number.isNaN(d.getTime())) return null;
  return `到期 ${d.toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}`;
}
function setListPage(kind: "events" | "checkin", delta: number): void {
  if (kind === "events") eventsPage.value = Math.min(eventPageCount.value, Math.max(1, eventsPage.value + delta));
  else checkinPage.value = Math.min(checkinPageCount.value, Math.max(1, checkinPage.value + delta));
}
function setPurpose(name: string, event: Event): void { if (name === "chat") draftChat.value = (event.target as HTMLInputElement).checked; else draftCheckin.value = (event.target as HTMLInputElement).checked; }

async function accountCheckinHistory(selectedProvider: string, selectedAccountId: string): Promise<CheckinAttempt[]> {
  const runs = await apiRequest<{ runs: { run_id: string }[] }>("/checkin/runs?limit=100");
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
      <section class="data-panel detail-section"><PanelHeader title="用途与路由" /><div class="form-grid"><label>显示名称<input v-model="draftLabel" :disabled="!canWrite" aria-label="账号显示名称" /></label><label class="inline-check"><input v-model="draftEnabled" type="checkbox" :disabled="!canWrite" />账号启用</label><label v-for="name in ['chat', 'checkin']" :key="name" class="inline-check"><input :checked="name === 'chat' ? draftChat : draftCheckin" type="checkbox" :disabled="!canWrite || !account.data.value.purposes[name]" @change="setPurpose(name, $event)" /><span>{{ statusLabel(name) }}</span><StatePill v-if="account.data.value.purposes[name]" :value="account.data.value.purposes[name].status" /></label></div><div class="purpose-cards"><div v-for="(item, name) in account.data.value.purposes" :key="name"><strong>{{ statusLabel(String(name)) }}</strong><StatePill :value="item.verification_status" /><small>到期 {{ item.expires_at ?? "未设置" }} · 验证 {{ item.verified_at ?? "尚未验证" }}<template v-if="item.last_error"> · {{ statusLabel(item.last_error) }}</template></small></div></div><div class="form-actions detail-save"><button type="button" :disabled="!canWrite" @click="requestAction('save')"><ShieldCheck :size="16" />保存设置</button></div></section>
      <div class="detail-main-grid"><section class="data-panel detail-section paged-section"><PanelHeader title="最近请求" /><div v-if="events.isPending.value" class="loading-row fixed-empty">正在读取请求…</div><div v-else-if="!events.data.value?.events.length" class="compact-empty fixed-empty">尚无请求事件。</div><template v-else><div class="metric-list metric-list--compact paged-list"><div v-for="event in visibleEvents" :key="event.event_id"><strong>{{ event.model_id ?? "未知模型" }}</strong><StatePill :value="event.status" /><span>{{ event.latency_ms ?? "--" }} ms<template v-if="event.error_code"> · {{ event.error_code }}</template></span><small>{{ event.started_at ?? "--" }}</small></div></div><div class="list-pagination"><span>第 {{ eventsPage }} / {{ eventPageCount }} 页</span><div><button class="secondary-button compact-button" type="button" :disabled="eventsPage <= 1" @click="setListPage('events', -1)">上一页</button><button class="secondary-button compact-button" type="button" :disabled="eventsPage >= eventPageCount" @click="setListPage('events', 1)">下一页</button></div></div></template></section><section class="data-panel detail-section paged-section"><PanelHeader title="签到历史" /><div v-if="checkin.isPending.value" class="loading-row fixed-empty">正在读取签到…</div><div v-else-if="!checkin.data.value?.length" class="compact-empty fixed-empty">尚无签到记录。</div><template v-else><div class="metric-list metric-list--compact paged-list"><div v-for="(item, index) in visibleCheckins" :key="`${item.finished_at}:${index}`"><strong>每日签到</strong><StatePill :value="item.outcome" /><span>{{ checkinQuotaHint(item) }}</span><small>{{ checkinReward(item) }}<template v-if="checkinDelta(item)"> · {{ checkinDelta(item) }}</template> · {{ item.finished_at ?? "--" }}</small></div></div><div class="list-pagination"><span>第 {{ checkinPage }} / {{ checkinPageCount }} 页</span><div><button class="secondary-button compact-button" type="button" :disabled="checkinPage <= 1" @click="setListPage('checkin', -1)">上一页</button><button class="secondary-button compact-button" type="button" :disabled="checkinPage >= checkinPageCount" @click="setListPage('checkin', 1)">下一页</button></div></div></template></section></div>
    </template>
    <OperationStatus :operation="lastOperation" />
    <ConfirmDialog :open="Boolean(pending)" :title="pending === 'delete' ? '删除这个账号？' : pending === 'verify' ? '验证并启用签到？' : '停用这个账号？'" :description="pending === 'delete' ? '账号的持久凭据和用途记录将被删除，操作不可撤销。' : pending === 'verify' ? '系统将使用当前账号的签到凭据或已登录 Chat 凭据发送一次每日签到请求；未签到时可能立即领取当天积分。' : '停用后该账号不会参与新的代理或签到调度。'" :confirm-label="pending === 'delete' ? '确认删除' : pending === 'verify' ? '确认并验证' : '确认停用'" :tone="pending === 'delete' ? 'danger' : 'default'" :verification-text="pending === 'delete' ? 'DELETE' : ''" :busy="action.isPending.value" @cancel="pending = null" @confirm="confirmPending" />
    <NotificationRegion :notifications="notifications" @dismiss="dismiss" />
  </section>
</template>

<style scoped>
.account-detail-page { gap: 14px; font-size: var(--text-sm); }
.account-detail-page h1 { margin-bottom: 4px; font-size: 18px; }
.account-detail-page .page-header p { font-size: 11px; }
.account-detail-page .panel-heading { min-height: 48px; padding: 10px 14px; }
.account-detail-page :deep(.panel-heading h2) { margin-bottom: 0; font-size: 14px; }
.account-detail-page :deep(.panel-heading p) { font-size: 11px; }
.account-detail-page :deep(.state-pill) { min-height: 22px; padding: 2px 7px; font-size: 11px; }
.account-detail-page .form-actions button { min-height: 32px; padding-inline: 11px; font-size: var(--text-sm); }
.account-detail-header { min-height: 64px; align-items: center; }
.account-summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border: 1px solid var(--line); background: var(--surface); }
.account-summary-item { display: grid; gap: 5px; min-height: 72px; align-content: center; padding: 12px 14px; border-right: 1px solid var(--line); }
.account-summary-item:last-child { border-right: 0; }
.account-summary-item span { color: var(--muted); font-size: var(--text-xs); }
.account-summary-item strong { color: var(--text); font-size: var(--text-sm); overflow-wrap: anywhere; }
.account-detail-page .form-grid { gap: 10px 16px; padding-top: 2px; }
.account-detail-page .form-grid > label:not(.inline-check) { font-size: 11px; }
.account-detail-page .form-grid > label:not(.inline-check) input { min-height: 32px; font-size: var(--text-sm); }
.account-detail-page .form-grid label.inline-check { display: flex; align-items: center; align-self: end; gap: 7px; min-height: 32px; padding: 0; color: var(--text); font-size: var(--text-sm); font-weight: 500; }
.account-detail-page .form-grid label.inline-check input[type="checkbox"] { appearance: none; width: 15px; height: 15px; flex: 0 0 15px; margin: 0; padding: 0; border: 1px solid var(--line-strong); border-radius: 2px; background: var(--canvas); }
.account-detail-page .form-grid label.inline-check input[type="checkbox"]:checked { border-color: var(--accent); background-color: var(--accent); background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath d='m3 8 3 3 7-7' fill='none' stroke='%230b0b0d' stroke-width='2'/%3E%3C/svg%3E"); background-position: center; background-repeat: no-repeat; background-size: 12px; }
.account-detail-page .form-grid label.inline-check input[type="checkbox"]:disabled { opacity: .45; }
.account-detail-page .purpose-cards { display: grid; gap: 0; margin-top: 10px; border-top: 1px solid var(--line); }
.account-detail-page .purpose-cards > div { display: grid; grid-template-columns: minmax(82px, auto) auto minmax(0, 1fr); align-items: center; gap: 6px 8px; min-height: 34px; padding: 5px 0; border-bottom: 1px solid var(--line); }
.account-detail-page .purpose-cards strong { font-size: var(--text-sm); }
.account-detail-page .purpose-cards small { min-width: 0; overflow: hidden; color: var(--muted); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.account-actions-panel .form-actions { min-height: 72px; padding: 0 16px 16px; }
.detail-section { overflow: hidden; }
.detail-save { justify-content: flex-end; margin-top: 10px; padding: 0 14px 12px; }
.detail-main-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(320px, 0.82fr); gap: 16px; min-width: 0; align-items: start; }
.fixed-empty { min-height: 152px; }
.detail-section .metric-list { min-height: 152px; }
.detail-main-grid > .detail-section { min-height: 210px; }
.account-detail-page .metric-list--compact > div { min-height: 44px; padding: 7px 14px; }
.account-detail-page .metric-list--compact > div > strong { font-size: var(--text-sm); }
.account-detail-page .metric-list--compact span, .account-detail-page .metric-list--compact small { font-size: 11px; }
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
