<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { Activity, ArrowLeft, BadgeCheck, KeyRound, Play, RefreshCcw, ShieldCheck, Trash2, RotateCcw } from "@lucide/vue";
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
type CheckinAttempt = { provider: string; account_id: string; outcome: string; finished_at?: string; error_code?: string; reward_credits?: number | null; reward_expires_at?: string | null; quota_after?: { packages?: CreditPackage[] } | null; quota_delta?: { packages?: { name?: string; delta?: number }[] } | null; quota_change_status?: string | null };
type GrowthTask = { task_code?: string; title?: string; task_desc?: string; task_type?: string; tag?: string; accept_status?: string; progress_current?: number | null; progress_target?: number | null; reward_credit?: number | null; reward_energy?: number | null; has_reward?: boolean | null; locked?: boolean | null; is_new?: boolean | null; icon_url?: string };
type GrowthProfile = { level?: number | null; completed?: number | null; total?: number | null; max_level?: boolean | null };
type HeatmapCell = { date?: string; score?: number | null; has_new_buddy?: boolean | null };
type GrowthHeatmap = { cells: HeatmapCell[]; today?: { date?: string; score?: number | null; is_active?: boolean | null; status_text?: string | null } | null; range_start?: string | null; range_end?: string | null };
type GrowthStreak = { days?: number | null; next_tier?: string | null; next_tier_remaining?: number | null; makeup_balance?: number | null; makeup_max?: number | null; remaining_days?: number | null; timezone?: string | null };
type GrowthLottery = { available_chances?: number | null; total_draws?: number | null };
type GrowthOverview = { profile: GrowthProfile; tasks: GrowthTask[]; heatmap: GrowthHeatmap; streak: GrowthStreak; lottery: GrowthLottery };
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
const packagesPage = ref(1);
const listPageSize = 10;
const { notifications, notify, dismiss } = useNotifications();
const isEnv = computed(() => account.data.value?.source === "env");
const lastGrowthResult = ref<Record<string, string> | null>(null);
const growthExecute = useMutation({
  mutationFn: () => apiRequest<{ status: string; result: Record<string, string> }>(`${base.value}/growth/execute`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }),
  onSuccess: async (data) => { lastGrowthResult.value = data.result ?? null; notify("成长自动化已执行", { message: Object.entries(data.result ?? {}).map(([k, v]) => `${automationLabel(k)}: ${v}`).join("；"), tone: "success" }); await growth.refetch(); },
  onError: (error) => notify("成长自动化执行失败", { message: String(error), tone: "error", timeout: 0 }),
});
function automationLabel(key: string): string { return { tasks: "任务", lottery: "抽奖", travel: "旅行", redeem: "兑换", buddy_open: "Buddy" }[key] ?? key; }

const account = useQuery({ queryKey: ["account-detail", provider, accountId], queryFn: () => apiRequest<Account>(base.value), staleTime: 15_000 });
const credentials = useQuery({ queryKey: ["account-credentials", provider, accountId], queryFn: async () => {
  const result = await apiRequest<{ credentials: Credential[] }>(`/credentials?provider=${encodeURIComponent(provider.value)}`);
  return result.credentials.filter((item) => item.account_id === accountId.value);
} });
const metrics = useQuery({ queryKey: ["account-metrics", provider, accountId], queryFn: () => apiRequest<{ snapshots: Metric[] }>(`/metrics/accounts/${encodeURIComponent(provider.value)}/${encodeURIComponent(accountId.value)}`) });
const events = useQuery({ queryKey: ["account-events", provider, accountId], queryFn: () => apiRequest<{ events: RequestEvent[] }>(`/usage/events?limit=100&provider=${encodeURIComponent(provider.value)}&account_id=${encodeURIComponent(accountId.value)}`) });
const checkin = useQuery({ queryKey: ["account-checkin", provider, accountId], queryFn: () => accountCheckinHistory(provider.value, accountId.value) });
const growth = useQuery({ queryKey: ["account-growth", provider, accountId], enabled: computed(() => provider.value === "codebuddy" && !isEnv.value), queryFn: () => apiRequest<GrowthOverview>(`${base.value}/growth`), staleTime: 30_000 });

watch(() => account.data.value, (value) => {
  if (!value) return;
  draftLabel.value = value.label;
  draftEnabled.value = value.enabled;
  draftChat.value = value.purposes.chat?.enabled ?? false;
  draftCheckin.value = value.purposes.checkin?.enabled ?? false;
}, { immediate: true });
watch([() => events.data.value?.events?.length, () => checkin.data.value?.length, () => metrics.data.value?.snapshots?.length], () => { eventsPage.value = 1; checkinPage.value = 1; packagesPage.value = 1; });

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
  if (code === "10001") return null;
  if (code === "checkin_failed") return null;
  return code ?? null;
}
function checkinQuotaHint(item: CheckinAttempt): string {
  if (item.quota_change_status === "claimed_balance_increased") return "刚刚领取成功";
  if (item.quota_change_status === "claimed_balance_unchanged") return "已领取，余额未变化";
  if (item.quota_change_status === "claimed_balance_pending") return "已领取，余额待刷新";
  if (item.quota_change_status === "already_checked_in") return "今日已签到";
  return checkinErrorHint(item.error_code) ?? "已完成";
}
function checkinReward(item: CheckinAttempt): string {
  const reward = typeof item.reward_credits === "number" ? `奖励 ${item.reward_credits.toLocaleString()} credits` : "未返回奖励";
  const expiry = formatExpiry(item.reward_expires_at ?? undefined);
  return expiry ? `${reward} · ${expiry}` : reward;
}
function checkinDelta(item: CheckinAttempt): string | null {
  const values = item.quota_delta?.packages?.filter((pkg) => typeof pkg.delta === "number") ?? [];
  if (!values.length) return null;
  return values.map((pkg) => `${pkg.name ?? "配额包"} ${pkg.delta! >= 0 ? "+" : ""}${pkg.delta}`).join(" · ");
}
function growthTaskStatus(task: GrowthTask): string {
  if (task.locked) return "locked";
  const done = typeof task.progress_current === "number" && typeof task.progress_target === "number" && task.progress_current >= task.progress_target;
  if (done && task.has_reward) return "claimable";
  if (done) return "completed";
  if (task.accept_status === "not_accepted") return "pending";
  return "in_progress";
}
function growthTaskLabel(status: string): string {
  return { locked: "已锁定", claimable: "可领奖", completed: "已完成", pending: "待接受", in_progress: "进行中" }[status] ?? status;
}
const heatmapGrid = computed(() => {
  const cells = growth.data.value?.heatmap?.cells ?? [];
  if (!cells.length) return { weeks: [] as HeatmapCell[][], monthLabel: "" };
  const start = new Date(cells[0].date ?? Date.now());
  const pad = (start.getDay() + 1) % 7;
  const padded: (HeatmapCell | null)[] = Array(pad).fill(null).concat(cells);
  const weeks: (HeatmapCell | null)[][] = [];
  for (let i = 0; i < padded.length; i += 7) weeks.push(padded.slice(i, i + 7));
  const last = cells[cells.length - 1]?.date ?? "";
  return { weeks, monthLabel: last.slice(0, 7) };
});
function cellLevel(cell: HeatmapCell | null): number {
  if (!cell || !cell.score) return 0;
  if (cell.score >= 60) return 4;
  if (cell.score >= 40) return 3;
  if (cell.score >= 20) return 2;
  return 1;
}
function cellTitle(cell: HeatmapCell | null): string {
  if (!cell) return "";
  const s = cell.score ? ` 活跃度 ${cell.score}` : " 未活跃";
  return `${cell.date}${s}${cell.has_new_buddy ? " · 获得 Buddy" : ""}`;
}
type MetricValue = {
  unit?: string;
  total_remaining?: number;
  total_used?: number;
  total_capacity?: number;
  total_usage_percentage?: number;
  expires_at?: string;
  packages?: { name?: string; remaining?: number; total?: number; used?: number; available?: number; cap?: number; unit?: string; expires_at?: string }[];
  user_quota?: QuotaDetail;
  add_on_quota?: QuotaDetail;
  org_resource_package?: QuotaDetail;
  activities?: { model?: string; tag?: string; limit?: number; used?: number; remaining?: number; reset_at?: number; activity_end_at?: number }[];
};
type QuotaDetail = { total?: number; used?: number; remaining?: number; percentage?: number; unit?: string; cap?: number; available?: number; expires_at?: string };
type MetricRow = { title: string; status: string; primary: string; secondary: string; details: string[] };
type CreditPackage = { name?: string; remaining?: number; total?: number; used?: number; available?: number; cap?: number; unit?: string; expires_at?: string };
function formatExpiry(expiresAt?: string): string | null {
  if (!expiresAt) return null;
  const d = new Date(expiresAt);
  if (Number.isNaN(d.getTime())) return null;
  return `到期 ${d.toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}`;
}
function metricRow(metric: Metric): MetricRow {
  const value = metric.value as MetricValue | null;
  const expiry = formatExpiry(value?.expires_at);
  const observed = metric.observed_at ?? "未记录采集时间";
  if (metric.metric_kind === "points" && value) {
    return { title: "积分", status: metric.status, primary: typeof value.total_remaining === "number" ? `${value.total_remaining.toLocaleString()} ${value.unit ?? "credits"}` : "未知", secondary: "", details: [] };
  }
  if (metric.metric_kind === "quota" && value) {
    const quotaDetails = [
      ["用户配额", value.user_quota],
      ["附加配额", value.add_on_quota],
    ].filter(([, detail]) => detail).map(([label, detail]) => `${label} · ${quotaSummary(detail as QuotaDetail)}`);
    return { title: "配额", status: metric.status, primary: typeof value.total_usage_percentage === "number" ? `已使用 ${value.total_usage_percentage}%` : quotaDetails.length ? "已返回配额" : "未知", secondary: quotaDetails.length ? "按配额包拆分展示" : "", details: [...quotaDetails, expiry, observed].filter(Boolean) as string[] };
  }
  if (metric.metric_kind === "activity" && value && Array.isArray(value.activities)) {
    const lines = value.activities.map((activity) => {
      const reset = activity.reset_at ? ` · 重置 ${formatEpoch(activity.reset_at)}` : "";
      const end = activity.activity_end_at ? ` · 活动止 ${formatEpoch(activity.activity_end_at)}` : "";
      return `${activity.model ?? "未知模型"} · 剩余 ${activity.remaining ?? "--"}/${activity.limit ?? "--"}${activity.tag ? ` · ${activity.tag}` : ""}${reset}${end}`;
    });
    return { title: "活动配额", status: metric.status, primary: `${value.activities.length} 个模型`, secondary: "免费活动剩余量", details: [...lines, observed] };
  }
  return { title: statusLabel(metric.metric_kind), status: metric.status, primary: metric.value ? "已采集" : "无数据", secondary: "", details: [observed] };
}
function quotaRemaining(value: QuotaDetail | undefined): number | null {
  if (!value) return null;
  const remaining = value.remaining ?? value.available;
  return typeof remaining === "number" && Number.isFinite(remaining) ? remaining : null;
}
const metricRows = computed<MetricRow[]>(() => {
  const snapshots = metrics.data.value?.snapshots ?? [];
  if (provider.value !== "qoder") return snapshots.map((metric) => metricRow(metric));

  const quotaMetric = snapshots.find((metric) => metric.metric_kind === "quota");
  const quotaValue = quotaMetric?.value as MetricValue | null | undefined;
  const quotaBalances = [
    quotaRemaining(quotaValue?.user_quota),
    quotaRemaining(quotaValue?.add_on_quota),
    quotaRemaining(quotaValue?.org_resource_package),
  ]
    .filter((value): value is number => value !== null);
  const pointsMetric = snapshots.find((metric) => metric.metric_kind === "points");
  const pointsValue = pointsMetric?.value as MetricValue | null | undefined;
  const packageBalances = (quotaValue?.packages ?? [])
    .map((item) => item.remaining ?? item.available)
    .filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  const additionalPackageBalances = (quotaValue?.packages ?? [])
    .filter((item) => !isQuotaSummaryPackage(item.name))
    .map((item) => item.remaining ?? item.available)
    .filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  const total = quotaBalances.length
    ? quotaBalances.reduce((sum, value) => sum + value, 0) + additionalPackageBalances.reduce((sum, value) => sum + value, 0)
    : packageBalances.length
      ? packageBalances.reduce((sum, value) => sum + value, 0)
      : (typeof quotaValue?.total_remaining === "number"
        ? quotaValue.total_remaining
        : typeof pointsValue?.total_remaining === "number" ? pointsValue.total_remaining : null);
  const rows: MetricRow[] = [];
  const quotaDetails = [
    ["用户积分", quotaValue?.user_quota],
    ["附加积分", quotaValue?.add_on_quota],
    ["组织积分", quotaValue?.org_resource_package],
  ]
    .filter(([, detail]) => detail)
    .map(([label, detail]) => `${label} · ${quotaSummary(detail as QuotaDetail)}`);
  if (quotaMetric || pointsMetric) rows.push({
    title: "积分",
    status: quotaMetric?.status ?? pointsMetric?.status ?? "unknown",
    primary: total === null ? "未知" : `${total.toLocaleString()} ${quotaValue?.unit ?? pointsValue?.unit ?? "credits"}`,
    secondary: quotaDetails.length ? "按积分包拆分" : "",
    details: quotaDetails,
  });
  rows.push(...snapshots
    .filter((metric) => metric.metric_kind !== "quota" && metric.metric_kind !== "points")
    .map((metric) => metricRow(metric)));
  return rows;
});
const checkinRewardPackages = computed<CreditPackage[]>(() => {
  if (provider.value !== "qoder") return [];
  return (checkin.data.value ?? []).flatMap((item, index) => {
    const recorded = (item.quota_after?.packages ?? []).filter((pkg) =>
      /签到|奖励|reward/i.test(pkg.name ?? "") ||
      (pkg.expires_at && item.reward_expires_at && pkg.expires_at === item.reward_expires_at),
    );
    const hasRecordedReward = recorded.some((pkg) =>
      (typeof pkg.remaining === "number" && pkg.remaining === item.reward_credits) ||
      (pkg.expires_at && pkg.expires_at === item.reward_expires_at),
    );
    const reward = typeof item.reward_credits === "number" && item.reward_credits > 0
      && !hasRecordedReward ? [{
        name: `签到奖励${item.finished_at ? ` · ${item.finished_at.slice(0, 10)}` : ` · ${index + 1}`}`,
        remaining: item.reward_credits,
        total: item.reward_credits,
        used: 0,
        unit: "credits",
        expires_at: item.reward_expires_at ?? undefined,
      }]
      : [];
    return [...recorded, ...reward];
  });
});
const creditPackages = computed<CreditPackage[]>(() => {
  const snapshots = metrics.data.value?.snapshots ?? [];
  const points = snapshots.find((metric) => metric.metric_kind === "points")?.value as MetricValue | null | undefined;
  const quota = snapshots.find((metric) => metric.metric_kind === "quota")?.value as MetricValue | null | undefined;
  const packages = provider.value === "qoder"
    ? [
      ...(quota?.packages ?? []).filter((item) => !isQuotaSummaryPackage(item.name)),
      ...(quota?.user_quota ? [{ name: "用户积分", ...quota.user_quota }] : []),
      ...(quota?.add_on_quota ? [{ name: "附加积分", ...quota.add_on_quota }] : []),
      ...(quota?.org_resource_package ? [{ name: "组织积分", ...quota.org_resource_package }] : []),
      ...checkinRewardPackages.value,
    ]
    : [...(points?.packages ?? [])];
  const seen = new Set<string>();
  return packages
    .map((item, index) => ({
      name: item.name ?? `积分包 ${index + 1}`,
      remaining: item.remaining ?? item.available,
      total: item.total ?? item.cap,
      used: item.used ?? (typeof item.total === "number" && typeof (item.remaining ?? item.available) === "number" ? item.total - (item.remaining ?? item.available)! : undefined),
      unit: item.unit ?? quota?.unit ?? points?.unit ?? "credits",
      expires_at: item.expires_at ?? (provider.value === "qoder" ? quota?.expires_at : undefined),
    }))
    .filter((item) => {
      const key = [item.name, item.total, item.used, item.remaining, item.expires_at].join("|");
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
});
function isQuotaSummaryPackage(name?: string): boolean {
  return ["user_quota", "add_on_quota", "org_resource_package"].includes(name ?? "");
}
const visibleCreditPackages = computed(() => creditPackages.value.slice((packagesPage.value - 1) * listPageSize, packagesPage.value * listPageSize));
const creditPackagePageCount = computed(() => Math.max(1, Math.ceil(creditPackages.value.length / listPageSize)));
function formatPackageAmount(value?: number): string {
  return typeof value === "number" ? value.toLocaleString() : "--";
}
function formatEpoch(ms?: number): string | null {
  if (typeof ms !== "number" || ms <= 0) return null;
  return new Date(ms).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
function quotaSummary(detail: QuotaDetail): string {
  const remaining = detail.remaining ?? detail.available;
  const total = detail.total ?? detail.cap;
  const usage = typeof detail.percentage === "number" ? ` · 已使用 ${detail.percentage}%` : "";
  const expiry = formatExpiry(detail.expires_at);
  const suffix = `${usage}${expiry ? ` · ${expiry}` : ""}`;
  if (remaining !== undefined && total !== undefined) return `剩余 ${remaining} / ${total}${detail.unit ? ` ${detail.unit}` : ""}${suffix}`;
  if (remaining !== undefined) return `剩余 ${remaining}${detail.unit ? ` ${detail.unit}` : ""}${suffix}`;
  return suffix ? suffix.replace(/^ · /, "") : "已返回";
}
function setListPage(kind: "events" | "checkin" | "packages", delta: number): void {
  if (kind === "events") eventsPage.value = Math.min(eventPageCount.value, Math.max(1, eventsPage.value + delta));
  else if (kind === "checkin") checkinPage.value = Math.min(checkinPageCount.value, Math.max(1, checkinPage.value + delta));
  else packagesPage.value = Math.min(creditPackagePageCount.value, Math.max(1, packagesPage.value + delta));
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
      <section class="data-panel detail-section"><PanelHeader title="用途与路由" /><div class="form-grid"><label>显示名称<input v-model="draftLabel" :disabled="!canWrite" aria-label="账号显示名称" /></label><label class="inline-check"><input v-model="draftEnabled" type="checkbox" :disabled="!canWrite" />账号启用</label><label v-for="name in ['chat', 'checkin']" :key="name" class="inline-check"><input :checked="name === 'chat' ? draftChat : draftCheckin" type="checkbox" :disabled="!canWrite || !account.data.value.purposes[name]" @change="setPurpose(name, $event)" /><span>{{ statusLabel(name) }}</span><StatePill v-if="account.data.value.purposes[name]" :value="account.data.value.purposes[name].status" /></label></div><div class="purpose-cards"><div v-for="(item, name) in account.data.value.purposes" :key="name"><strong>{{ statusLabel(String(name)) }}</strong><StatePill :value="item.verification_status" /><small>到期 {{ item.expires_at ?? "未设置" }} · 验证 {{ item.verified_at ?? "尚未验证" }}<template v-if="item.last_error"> · {{ item.last_error }}</template></small></div></div><div class="form-actions detail-save"><button type="button" :disabled="!canWrite" @click="requestAction('save')"><ShieldCheck :size="16" />保存设置</button></div></section>
      <div class="detail-main-grid"><section class="data-panel detail-section"><PanelHeader title="积分与配额" /><div v-if="metrics.isPending.value" class="loading-row fixed-empty">正在读取指标…</div><div v-else-if="!metricRows.length" class="compact-empty fixed-empty">尚未采集指标。</div><div v-else class="metric-list metric-list--compact"><div v-for="row in metricRows" :key="row.title"><strong>{{ row.title }}</strong><StatePill :value="row.status" /><span class="metric-primary">{{ row.primary }}</span><small v-if="row.secondary">{{ row.secondary }}</small><small v-for="detail in row.details" :key="detail">{{ detail }}</small></div></div></section><section class="data-panel detail-section"><PanelHeader title="凭据元数据" /><div v-if="credentials.isPending.value" class="loading-row fixed-empty">正在读取凭据…</div><div v-else-if="!credentials.data.value?.length" class="compact-empty fixed-empty">尚未保存凭据。</div><div v-else class="metric-list metric-list--compact"><div v-for="item in credentials.data.value" :key="item.purpose"><strong>{{ statusLabel(item.purpose) }} · {{ item.mode }}</strong><StatePill :value="item.has_refresh_token ? 'refresh' : 'static'" /><span>版本 v{{ item.credential_version }} · 到期 {{ item.expires_at ?? "未设置" }}</span><small>{{ item.updated_at }}</small></div></div></section></div>
      <section class="data-panel detail-section points-detail-section paged-section"><PanelHeader title="积分明细" /><div v-if="metrics.isPending.value" class="loading-row fixed-empty">正在读取积分明细…</div><div v-else-if="!creditPackages.length" class="compact-empty fixed-empty">尚无积分包明细。</div><template v-else><div class="table-wrap"><table class="data-table credits-detail-table"><thead><tr><th>名称</th><th>总量</th><th>已用</th><th>剩余</th><th>到期时间</th></tr></thead><tbody><tr v-for="(item, index) in visibleCreditPackages" :key="`${item.name}-${index}`"><td><strong>{{ item.name }}</strong></td><td>{{ formatPackageAmount(item.total) }}<small>{{ item.unit }}</small></td><td>{{ formatPackageAmount(item.used) }}</td><td>{{ formatPackageAmount(item.remaining) }}</td><td>{{ formatExpiry(item.expires_at)?.replace(/^到期 /, "") ?? "未设置" }}</td></tr></tbody></table></div><div class="list-pagination"><span>第 {{ packagesPage }} / {{ creditPackagePageCount }} 页</span><div><button class="secondary-button compact-button" type="button" :disabled="packagesPage <= 1" @click="setListPage('packages', -1)">上一页</button><button class="secondary-button compact-button" type="button" :disabled="packagesPage >= creditPackagePageCount" @click="setListPage('packages', 1)">下一页</button></div></div></template></section>
      <section v-if="provider === 'codebuddy' && !isEnv" class="data-panel detail-section growth-section"><PanelHeader title="成长中心" description="实时拉取 WorkBuddy 成长计划任务状态。" /><div class="growth-toolbar"><span v-if="growth.data.value?.profile" class="growth-level">等级 {{ growth.data.value.profile.level ?? "--" }} · 已完成 {{ growth.data.value.profile.completed ?? "--" }}/{{ growth.data.value.profile.total ?? "--" }}</span><div class="growth-toolbar-actions"><button class="secondary-button compact-button" type="button" :disabled="growthExecute.isPending.value" @click="growthExecute.mutate()"><Play :size="14" />执行自动化</button><button class="secondary-button compact-button" type="button" :disabled="growth.isFetching.value" @click="growth.refetch()"><RefreshCcw :class="{ spin: growth.isFetching.value }" :size="14" />刷新</button></div></div><div v-if="lastGrowthResult" class="growth-automation-status"><small v-for="(val, key) in lastGrowthResult" :key="String(key)"><strong>{{ automationLabel(String(key)) }}</strong> {{ val }}</small></div><div v-if="growth.isPending.value" class="loading-row growth-empty">正在读取成长任务…</div><div v-else-if="growth.isError.value" class="data-state data-state--warning growth-empty">成长任务读取失败：{{ growth.error.value }}<button class="secondary-button compact-button" type="button" @click="growth.refetch()">重试</button></div><div v-else-if="!growth.data.value?.tasks?.length" class="compact-empty growth-empty">暂无成长任务。</div><div v-else class="growth-task-list"><div v-for="task in growth.data.value.tasks" :key="task.task_code" class="growth-task" :class="`growth-task--${growthTaskStatus(task)}`"><div v-if="task.icon_url" class="growth-task-icon"><img :src="task.icon_url" :alt="task.title" loading="lazy" /></div><div class="growth-task-body"><strong>{{ task.title ?? task.task_code }}</strong><small v-if="task.task_desc">{{ task.task_desc }}</small><span class="growth-task-meta"><StatePill :value="growthTaskStatus(task)" /> {{ growthTaskLabel(growthTaskStatus(task)) }}<template v-if="typeof task.progress_current === 'number' && typeof task.progress_target === 'number'"> · 进度 {{ task.progress_current }}/{{ task.progress_target }}</template><template v-if="task.reward_credit"> · 奖励 {{ task.reward_credit }} 积分</template><template v-if="task.reward_energy"> · {{ task.reward_energy }} 能量</template><template v-if="task.tag"> · {{ task.tag }}</template></span></div></div></div></section>
      <section v-if="provider === 'codebuddy' && !isEnv && growth.data.value" class="data-panel detail-section streak-section"><PanelHeader title="连登地图" description="活跃热力图 · 连续登录天数 · 抽奖机会。" /><div class="streak-toolbar"><div v-if="growth.data.value.streak" class="streak-stats"><span class="streak-days">连登 {{ growth.data.value.streak.days ?? 0 }} 天</span><template v-if="growth.data.value.streak.next_tier"> · 距 {{ growth.data.value.streak.next_tier }} 还差 {{ growth.data.value.streak.next_tier_remaining ?? "--" }} 天</template><template v-if="growth.data.value.streak.makeup_balance !== null"> · 补登卡 {{ growth.data.value.streak.makeup_balance ?? 0 }}/{{ growth.data.value.streak.makeup_max ?? 4 }}</template></div><div v-if="growth.data.value.lottery && (growth.data.value.lottery.available_chances || 0) > 0" class="streak-lottery">🎲 可抽奖 {{ growth.data.value.lottery.available_chances }} 次</div></div><div v-if="growth.data.value.heatmap?.today" class="streak-today" :class="{ active: growth.data.value.heatmap.today.is_active }">{{ growth.data.value.heatmap.today.status_text ?? (growth.data.value.heatmap.today.is_active ? "今日已活跃" : "今日未活跃") }}</div><div v-if="heatmapGrid.weeks.length" class="heatmap-grid"><div v-for="(week, wi) in heatmapGrid.weeks" :key="wi" class="heatmap-week"><div v-for="(cell, ci) in week" :key="ci" class="heatmap-cell" :class="`heatmap-cell--lvl${cellLevel(cell)}`" :title="cellTitle(cell)"></div></div></div><div class="heatmap-legend"><span class="heatmap-legend-label">少</span><div class="heatmap-cell heatmap-cell--lvl0"></div><div class="heatmap-cell heatmap-cell--lvl1"></div><div class="heatmap-cell heatmap-cell--lvl2"></div><div class="heatmap-cell heatmap-cell--lvl3"></div><div class="heatmap-cell heatmap-cell--lvl4"></div><span class="heatmap-legend-label">多</span></div></section>
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
.points-detail-section { min-height: 210px; }
.credits-detail-table { min-width: 620px; }
.credits-detail-table th, .credits-detail-table td { padding: 8px 12px; font-size: 11px; white-space: nowrap; }
.credits-detail-table th { color: var(--muted); font-size: 10px; font-weight: 600; letter-spacing: .02em; }
.credits-detail-table td strong { color: var(--text); font-size: 12px; font-weight: 600; }
.credits-detail-table td small { display: block; margin-top: 2px; color: var(--faint); font-size: 10px; }
.detail-section .metric-list { min-height: 152px; }
.detail-main-grid > .detail-section { min-height: 210px; }
.account-detail-page .metric-list--compact > div { min-height: 44px; padding: 7px 14px; }
.account-detail-page .metric-list--compact > div > strong { font-size: var(--text-sm); }
.account-detail-page .metric-list--compact .metric-primary { font-size: var(--text-sm); }
.account-detail-page .metric-list--compact span, .account-detail-page .metric-list--compact small { font-size: 11px; }
.growth-section { min-height: 180px; }
.growth-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 8px 14px; border-bottom: 1px solid var(--line); }
.growth-toolbar-actions { display: flex; gap: 6px; }
.growth-automation-status { display: flex; flex-wrap: wrap; gap: 6px 12px; padding: 6px 14px; border-bottom: 1px solid var(--line); background: var(--surface-raised); }
.growth-automation-status small { color: var(--muted); font-size: 11px; }
.growth-automation-status small strong { color: var(--accent); margin-right: 4px; }
.growth-level { color: var(--accent); font-size: 12px; font-weight: 600; font-variant-numeric: tabular-nums; }
.growth-empty { min-height: 120px; }
.growth-task-list { display: grid; gap: 0; padding: 0; }
.growth-task { display: grid; grid-template-columns: 32px minmax(0, 1fr); gap: 10px; align-items: start; min-height: 56px; padding: 10px 14px; border-bottom: 1px solid var(--line); }
.growth-task:last-child { border-bottom: 0; }
.growth-task-icon { width: 32px; height: 32px; overflow: hidden; border-radius: 2px; background: var(--surface-muted); }
.growth-task-icon img { width: 100%; height: 100%; object-fit: cover; }
.growth-task-body { display: grid; gap: 3px; min-width: 0; }
.growth-task-body strong { font-size: 12px; color: var(--text); overflow-wrap: anywhere; }
.growth-task-body small { color: var(--muted); font-size: 11px; line-height: 1.5; }
.growth-task-meta { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; margin-top: 2px; color: var(--faint); font-size: 11px; }
.growth-task--completed { opacity: 0.7; }
.growth-task--claimable { border-left: 2px solid var(--accent); }
.growth-task--locked { opacity: 0.42; }
.streak-section { padding-bottom: 12px; }
.streak-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 14px; border-bottom: 1px solid var(--line); }
.streak-stats { color: var(--muted); font-size: 12px; }
.streak-days { color: var(--accent); font-weight: 600; font-size: 14px; font-variant-numeric: tabular-nums; }
.streak-lottery { color: var(--ok); font-size: 12px; font-weight: 600; }
.streak-today { margin: 8px 14px 4px; padding: 6px 10px; border-radius: 2px; background: var(--surface-muted); color: var(--faint); font-size: 11px; }
.streak-today.active { background: var(--ok-soft); color: var(--ok); border: 1px solid var(--ok-line); }
.heatmap-grid { display: flex; gap: 3px; padding: 8px 14px 4px; overflow-x: auto; }
.heatmap-week { display: grid; grid-template-rows: repeat(7, 11px); gap: 3px; }
.heatmap-cell { width: 11px; height: 11px; border-radius: 2px; background: var(--surface-muted); border: 1px solid var(--line); }
.heatmap-cell--lvl1 { background: rgb(232 145 58 / 0.25); border-color: transparent; }
.heatmap-cell--lvl2 { background: rgb(232 145 58 / 0.45); border-color: transparent; }
.heatmap-cell--lvl3 { background: rgb(232 145 58 / 0.7); border-color: transparent; }
.heatmap-cell--lvl4 { background: var(--accent); border-color: transparent; }
.heatmap-legend { display: flex; align-items: center; gap: 4px; padding: 4px 14px 8px; }
.heatmap-legend-label { color: var(--faint); font-size: 10px; }
.heatmap-legend .heatmap-cell { width: 10px; height: 10px; }
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
