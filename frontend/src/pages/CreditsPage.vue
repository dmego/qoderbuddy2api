<script setup lang="ts">
import { useMutation, useQuery } from "@tanstack/vue-query";
import { Activity, CalendarClock, Coins, RefreshCcw, RotateCcw, SlidersHorizontal, X } from "@lucide/vue";
import { computed, ref } from "vue";

import { apiRequest } from "@/api/client";
import MetricChart from "@/components/MetricChart.vue";
import NotificationRegion from "@/components/NotificationRegion.vue";
import OperationStatus from "@/components/OperationStatus.vue";
import PanelHeader from "@/components/PanelHeader.vue";
import StatePill from "@/components/StatePill.vue";
import { useNotifications } from "@/composables/useNotifications";

type Account = { provider: string; account_id: string; label: string; enabled: boolean; summary_status: string; masked_identity?: string };
type AccountPage = { accounts: Account[]; next_cursor?: string | number | null };
type Metric = { provider: string; account_id: string; metric_kind: string; status: string; observed_at?: string; value: Record<string, unknown> | null };
type HistoryRow = { observed_at: string; status: string; value: Record<string, unknown> | null };
type HistoryResult = { key: string; provider: string; account_id: string; rows: HistoryRow[]; error?: string };
type Preset = "24h" | "7d" | "30d" | "custom";
type QuotaDetail = { total?: number; used?: number; remaining?: number; available?: number; percentage?: number; unit?: string; cap?: number; expires_at?: string };
type CreditPackage = { name?: string; remaining?: number; total?: number; used?: number; available?: number; cap?: number; unit?: string; expires_at?: string };
type MetricValue = {
  unit?: string; total_remaining?: number; expires_at?: string;
  packages?: CreditPackage[];
  user_quota?: QuotaDetail; add_on_quota?: QuotaDetail; org_resource_package?: QuotaDetail;
};

const provider = ref("");
const accountSearch = ref("");
const selectedAccount = ref("");
const preset = ref<Preset>("7d");
const customFrom = ref("");
const customTo = ref("");
const refreshInterval = ref(0);
const lastOperation = ref<Record<string, unknown> | null>(null);
const { notifications, notify, dismiss } = useNotifications();

const accounts = useQuery({ queryKey: ["credits-accounts"], queryFn: fetchAllAccounts, staleTime: 30_000 });
const metrics = useQuery({ queryKey: ["credits-metrics", provider], queryFn: () => apiRequest<{ snapshots: Metric[] }>(`/metrics/accounts?limit=500${provider.value ? `&provider=${encodeURIComponent(provider.value)}` : ""}`), staleTime: 15_000, refetchInterval: () => refreshInterval.value || false });

const visibleAccounts = computed(() => (accounts.data.value?.accounts ?? []).filter((item) => {
  const matchesProvider = !provider.value || item.provider === provider.value;
  const query = accountSearch.value.trim().toLowerCase();
  return matchesProvider && (!query || `${item.label} ${item.account_id}`.toLowerCase().includes(query));
}));
const bounds = computed(() => {
  if (preset.value === "custom") return { from: customFrom.value ? toIso(customFrom.value) : "", to: customTo.value ? toIso(customTo.value) : "" };
  const hours = preset.value === "24h" ? 24 : preset.value === "30d" ? 24 * 30 : 24 * 7;
  return { from: new Date(Date.now() - hours * 60 * 60 * 1000).toISOString(), to: "" };
});
const historyKeys = computed(() => visibleAccounts.value.map((item) => `${item.provider}:${item.account_id}`).sort().join(","));
const histories = useQuery({
  queryKey: ["credits-history", historyKeys, bounds],
  enabled: computed(() => visibleAccounts.value.length > 0),
  queryFn: async (): Promise<HistoryResult[]> => Promise.all(visibleAccounts.value.slice(0, 200).map(async (account) => {
    const key = `${account.provider}:${account.account_id}`;
    try {
      const metricKind = account.provider === "qoder" ? "quota" : "points";
      const result = await apiRequest<{ rows: HistoryRow[] }>(`/metrics/accounts/${encodeURIComponent(account.provider)}/${encodeURIComponent(account.account_id)}/history/${metricKind}?limit=500&since=${encodeURIComponent(bounds.value.from)}`);
      return { key, provider: account.provider, account_id: account.account_id, rows: result.rows };
    } catch (error) {
      return { key, provider: account.provider, account_id: account.account_id, rows: [], error: error instanceof Error ? error.message : String(error) };
    }
  })),
  staleTime: 30_000,
  refetchInterval: () => refreshInterval.value || false,
});

const currentRows = computed(() => {
  const latest = new Map<string, Metric>();
  for (const metric of metrics.data.value?.snapshots ?? []) {
    if (metricRemaining(metric) === null) continue;
    latest.set(`${metric.provider}:${metric.account_id}`, metric);
  }
  return visibleAccounts.value.map((account) => {
    const metric = latest.get(`${account.provider}:${account.account_id}`);
    const total = metric ? metricRemaining(metric) : null;
    const history = histories.data.value?.find((item) => item.key === `${account.provider}:${account.account_id}`)?.rows ?? [];
    const values = historyValues(history, bounds.value, account.provider);
    const first = values[0]?.value;
    return { account, metric, total, unit: metricUnit(metric), change: typeof total === "number" && typeof first === "number" ? total - first : null, observedAt: metric?.observed_at ?? "", status: metric?.status ?? "unavailable" };
  });
});
const chart = computed(() => {
  const rows = histories.data.value ?? [];
  const selected = selectedAccount.value ? rows.filter((item) => item.key === selectedAccount.value) : rows;
  const points = new Map<string, number>();
  for (const item of selected) for (const row of item.rows) {
    if (!withinBounds(row.observed_at, bounds.value)) continue;
    const value = historyRemaining(row.value, item.provider);
    if (value === null) continue;
    const key = row.observed_at.slice(0, 16);
    points.set(key, (points.get(key) ?? 0) + value);
  }
  const sorted = [...points.entries()].sort(([a], [b]) => a.localeCompare(b));
  return { labels: sorted.map(([key]) => key.replace("T", " ").slice(5)), values: sorted.map(([, value]) => value) };
});
const summary = computed(() => {
  const available = currentRows.value.filter((item) => item.total !== null);
  const total = available.length ? available.reduce((sum, item) => sum + (item.total ?? 0), 0) : null;
  const changes = available.filter((item) => item.change !== null).map((item) => item.change as number);
  const observed = available.map((item) => item.observedAt).filter(Boolean).sort().at(-1);
  return { total, accountCount: available.length, change: changes.length ? changes.reduce((sum, value) => sum + value, 0) : null, observed };
});
const partialUnavailable = computed(() => histories.data.value?.some((item) => item.error) ?? false);

const refresh = useMutation({
  mutationFn: async () => {
    const started = await apiRequest<{ operation_id: string }>("/metrics/refresh", { method: "POST" });
    for (let attempt = 0; attempt < 30; attempt += 1) {
      const result = await apiRequest<Record<string, unknown>>(`/metrics/refresh/${encodeURIComponent(started.operation_id)}`);
      if (result.status !== "running") return result;
      await new Promise((resolve) => window.setTimeout(resolve, 500));
    }
    throw new Error("指标刷新仍在运行，请稍后查看");
  },
  onSuccess: async (result) => {
    const status = result.status === "succeeded" ? "succeeded" : "failed";
    lastOperation.value = { action: "刷新积分指标", status, ...result };
    if (status === "succeeded") { notify("积分指标已刷新", { tone: "success" }); await refreshData(); }
    else notify("积分指标刷新未成功", { message: String(result.error_code ?? "metrics_refresh_failed"), tone: "error", timeout: 0 });
  },
  onError: (error) => notify("积分指标刷新失败", { message: String(error), tone: "error", timeout: 0 }),
});

async function fetchAllAccounts(): Promise<AccountPage> {
  const all: Account[] = [];
  let cursor: string | number = "";
  do {
    const endpoint: string = `/accounts?limit=100${cursor ? `&cursor=${encodeURIComponent(String(cursor))}` : ""}`;
    const page: AccountPage = await apiRequest<AccountPage>(endpoint);
    all.push(...page.accounts);
    cursor = page.next_cursor ?? "";
  } while (cursor);
  return { accounts: all, next_cursor: null };
}
function metricRemaining(metric: Metric): number | null {
  if (metric.metric_kind === "points") return numberValue(metric.value?.total_remaining);
  if (metric.metric_kind !== "quota") return null;
  return quotaRemaining(metric.value);
}
function metricUnit(metric: Metric | undefined): string {
  if (!metric?.value) return "credits";
  if (typeof metric.value.unit === "string") return metric.value.unit;
  const quota = metric.value.user_quota;
  return quota && typeof quota === "object" && typeof (quota as Record<string, unknown>).unit === "string" ? String((quota as Record<string, unknown>).unit) : "credits";
}
function historyRemaining(value: Record<string, unknown> | null, providerName: string): number | null {
  if (providerName === "qoder") return quotaRemaining(value);
  return numberValue(value?.total_remaining);
}
function quotaRemaining(value: Record<string, unknown> | null): number | null {
  if (!value) return null;
  const parts = [value.user_quota, value.add_on_quota].map((item) => item && typeof item === "object" ? numberValue((item as Record<string, unknown>).remaining) : null).filter((item): item is number => item !== null);
  return parts.length ? parts.reduce((sum, item) => sum + item, 0) : numberValue(value.remaining);
}
function numberValue(value: unknown): number | null { return typeof value === "number" && Number.isFinite(value) ? value : null; }
function historyValues(rows: HistoryRow[], range: { from: string; to: string }, providerName: string): { at: string; value: number }[] { return rows.filter((row) => withinBounds(row.observed_at, range)).map((row) => ({ at: row.observed_at, value: historyRemaining(row.value, providerName) })).filter((item): item is { at: string; value: number } => typeof item.value === "number"); }
function withinBounds(observedAt: string, range: { from: string; to: string }): boolean { return (!range.from || observedAt >= range.from) && (!range.to || observedAt <= range.to); }
function toIso(value: string): string { if (!value) return new Date(Date.now() - 7 * 86400000).toISOString(); const date = new Date(value); return Number.isNaN(date.valueOf()) ? "" : date.toISOString(); }
function formatNumber(value: number | null): string { return value === null ? "--" : value.toLocaleString(); }
function formatChange(value: number | null): string { return value === null ? "--" : `${value > 0 ? "+" : ""}${value.toLocaleString()}`; }
function accountLabel(key: string): string { const item = accounts.data.value?.accounts.find((account) => `${account.provider}:${account.account_id}` === key); return item?.label ?? key; }
function clearFilters(): void { provider.value = ""; accountSearch.value = ""; selectedAccount.value = ""; preset.value = "7d"; customFrom.value = ""; customTo.value = ""; }
async function refreshData(): Promise<void> { await Promise.all([accounts.refetch(), metrics.refetch(), histories.refetch()]); }
async function refreshView(): Promise<void> { await refreshData(); notify("积分视图已刷新", { tone: "info" }); }

function isQuotaSummaryPackage(name?: string): boolean { return ["user_quota", "add_on_quota", "org_resource_package"].includes(name ?? ""); }
function dedupePackages(packages: CreditPackage[]): CreditPackage[] {
  const seen = new Set<string>();
  return packages.filter((item) => {
    const key = [item.name, item.total, item.used, item.remaining, item.expires_at].join("|");
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
function formatPackageAmount(value?: number): string { return typeof value === "number" ? value.toLocaleString() : "--"; }
function formatExpiry(expiresAt?: string): string | null {
  if (!expiresAt) return null;
  const d = new Date(expiresAt);
  return Number.isNaN(d.getTime()) ? null : d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

// 积分明细弹窗
const detailAccount = ref("");
const detailPage = ref(1);
const detailPageSize = 8;
const detailVisible = computed(() => Boolean(detailAccount.value));
const detailMetric = computed(() => {
  if (!detailAccount.value) return null;
  const [p, aid] = detailAccount.value.split(":");
  return metrics.data.value?.snapshots?.find((m) => m.provider === p && m.account_id === aid) ?? null;
});
const detailPackages = computed<CreditPackage[]>(() => {
  const value = detailMetric.value?.value as MetricValue | null | undefined;
  if (!value) return [];
  const providerName = detailAccount.value.split(":")[0];
  if (providerName === "qoder") {
    const quota = value;
    return dedupePackages([
      ...(quota.packages ?? []).filter((item) => !isQuotaSummaryPackage(item.name)),
      ...(quota.user_quota ? [{ name: "用户积分", ...quota.user_quota }] : []),
      ...(quota.add_on_quota ? [{ name: "附加积分", ...quota.add_on_quota }] : []),
      ...(quota.org_resource_package ? [{ name: "组织积分", ...quota.org_resource_package }] : []),
    ]);
  }
  return dedupePackages(value.packages ?? []);
});
const detailPageCount = computed(() => Math.max(1, Math.ceil(detailPackages.value.length / detailPageSize)));
const detailVisiblePackages = computed(() => detailPackages.value.slice((detailPage.value - 1) * detailPageSize, detailPage.value * detailPageSize));
const detailAccountLabel = computed(() => {
  const [p, aid] = detailAccount.value.split(":");
  const item = accounts.data.value?.accounts.find((a) => a.provider === p && a.account_id === aid);
  return item?.label ?? detailAccount.value;
});
function openDetail(key: string): void {
  detailAccount.value = key;
  detailPage.value = 1;
}
function closeDetail(): void { detailAccount.value = ""; }
function setDetailPage(delta: number): void {
  detailPage.value = Math.min(detailPageCount.value, Math.max(1, detailPage.value + delta));
}
</script>

<template>
  <section class="page-content">
    <header class="page-header"><div><h1>积分监控</h1><p>查看账号池当前积分、变化曲线与采集状态；未知值保持不可用，不会伪装成 0。</p></div><div class="header-actions"><label class="refresh-control"><span>自动刷新</span><select v-model.number="refreshInterval" aria-label="自动刷新间隔"><option :value="0">关闭</option><option :value="30000">30 秒</option><option :value="60000">1 分钟</option><option :value="300000">5 分钟</option></select></label><button class="secondary-button" type="button" :disabled="refresh.isPending.value || metrics.isFetching.value" @click="refreshView"><RefreshCcw :class="{ spin: metrics.isFetching.value }" :size="16" />刷新视图</button><button type="button" :disabled="refresh.isPending.value" @click="refresh.mutate()"><RefreshCcw :class="{ spin: refresh.isPending.value }" :size="16" />{{ refresh.isPending.value ? "刷新中" : "立即刷新积分" }}</button></div></header>

    <section class="data-panel credits-filters"><PanelHeader title="积分筛选" description="筛选条件同时作用于摘要、趋势和账号列表。"><button class="secondary-button compact-button" type="button" @click="clearFilters"><X :size="14" />清除</button></PanelHeader><div class="usage-filter-grid"><label>服务提供方<select v-model="provider" aria-label="服务提供方"><option value="">全部服务提供方</option><option value="codebuddy">CodeBuddy</option><option value="qoder">Qoder</option></select></label><label>账号搜索<input v-model.trim="accountSearch" aria-label="账号搜索" placeholder="名称或账号 ID" /></label><label>趋势账号<select v-model="selectedAccount" aria-label="趋势账号"><option value="">全部账号汇总</option><option v-for="item in visibleAccounts" :key="`${item.provider}:${item.account_id}`" :value="`${item.provider}:${item.account_id}`">{{ item.label }} · {{ item.provider }}</option></select></label><label>时间窗口<select v-model="preset" aria-label="时间窗口"><option value="24h">最近 24 小时</option><option value="7d">最近 7 天</option><option value="30d">最近 30 天</option><option value="custom">自定义</option></select></label><label v-if="preset === 'custom'">开始时间<input v-model="customFrom" type="datetime-local" aria-label="开始时间" /></label><label v-if="preset === 'custom'">结束时间<input v-model="customTo" type="datetime-local" aria-label="结束时间" /></label></div></section>

    <div v-if="partialUnavailable" class="data-state data-state--warning" role="status">部分账号的积分历史暂不可用，页面仍展示其余账号数据。</div>
    <div class="summary-grid summary-grid--five" :aria-busy="metrics.isPending.value"><article class="summary-tile"><Coins :size="18" /><span>当前积分总量</span><strong>{{ formatNumber(summary.total) }}</strong><small>可用账号 {{ summary.accountCount }}</small></article><article class="summary-tile"><Activity :size="18" /><span>窗口内变化</span><strong :class="(summary.change ?? 0) < 0 ? 'value-negative' : 'value-positive'">{{ formatChange(summary.change) }}</strong><small>{{ preset === 'custom' ? '自定义窗口' : `最近 ${preset === '24h' ? '24 小时' : preset === '7d' ? '7 天' : '30 天'}` }}</small></article><article class="summary-tile"><CalendarClock :size="18" /><span>最近采集</span><strong class="summary-date">{{ summary.observed ? summary.observed.slice(5, 16).replace('T', ' ') : '--' }}</strong><small>按账号最新快照</small></article><article class="summary-tile"><SlidersHorizontal :size="18" /><span>历史采样点</span><strong>{{ chart.values.length.toLocaleString() }}</strong><small>{{ selectedAccount ? accountLabel(selectedAccount) : '全部账号汇总' }}</small></article><article class="summary-tile"><RotateCcw :size="18" /><span>采集状态</span><strong><StatePill :value="metrics.isError.value ? 'unavailable' : metrics.isStale.value ? 'stale' : 'fresh'" /></strong><small>自动刷新 {{ refreshInterval ? '已开启' : '已关闭' }}</small></article></div>

    <section class="data-panel"><PanelHeader title="积分变化曲线" :description="`${selectedAccount ? accountLabel(selectedAccount) : '全部账号汇总'} · ${chart.values.length} 个有效采样点`" /><div v-if="histories.isPending.value" class="loading-row">正在读取积分历史…</div><div v-else-if="histories.isError.value" class="data-state data-state--error">积分历史读取失败。<button class="secondary-button compact-button" type="button" @click="histories.refetch()">重试</button></div><div v-else-if="!chart.labels.length" class="compact-empty">当前筛选时间内没有可用积分采样点。</div><MetricChart v-else :labels="chart.labels" :values="chart.values" /></section>

    <section class="data-panel"><PanelHeader title="账号积分" :description="`${currentRows.length} 个账号 · 最近快照`" /><div v-if="accounts.isPending.value || metrics.isPending.value" class="loading-row">正在读取账号积分…</div><div v-else-if="!currentRows.length" class="compact-empty">没有匹配的账号。</div><div v-else class="table-wrap"><table class="data-table credits-table"><thead><tr><th>账号</th><th>服务商</th><th>当前积分</th><th>窗口变化</th><th>采集时间</th><th>状态</th></tr></thead><tbody><tr v-for="row in currentRows" :key="`${row.account.provider}:${row.account.account_id}`" :class="{ selected: selectedAccount === `${row.account.provider}:${row.account.account_id}` }" @click="selectedAccount = `${row.account.provider}:${row.account.account_id}`"><td><button class="table-link" type="button" @click.stop="openDetail(`${row.account.provider}:${row.account.account_id}`)"><strong>{{ row.account.label }}</strong><small>{{ row.account.account_id }} · {{ row.account.masked_identity ?? '无身份掩码' }}</small></button></td><td><span class="provider-mark" :class="`provider-mark--${row.account.provider}`">{{ row.account.provider }}</span></td><td><strong class="credit-value">{{ formatNumber(row.total) }}</strong><small>{{ row.unit }}</small></td><td :class="row.change !== null && row.change < 0 ? 'value-negative' : 'value-positive'"><span v-if="row.change !== null">{{ row.change < 0 ? '↓' : '↑' }} {{ formatChange(row.change) }}</span><span v-else>--</span></td><td>{{ row.observedAt ? row.observedAt.slice(5, 16).replace('T', ' ') : '--' }}</td><td><StatePill :value="row.status" /></td></tr></tbody></table></div></section>

    <div v-if="detailVisible" class="credits-detail-modal" role="dialog" aria-modal="true" aria-label="积分明细">
      <div class="credits-detail-backdrop" @click="closeDetail" />
      <div class="credits-detail-dialog">
        <header class="credits-detail-header"><h2>{{ detailAccountLabel }} · 积分明细</h2><button class="icon-button" type="button" aria-label="关闭" @click="closeDetail"><X :size="18" /></button></header>
        <div class="credits-detail-body">
          <div v-if="!detailPackages.length" class="compact-empty">尚无积分包明细。</div>
          <div v-else class="table-wrap"><table class="data-table credits-detail-table"><thead><tr><th>名称</th><th>总量</th><th>已用</th><th>剩余</th><th>到期</th></tr></thead><tbody><tr v-for="(item, index) in detailVisiblePackages" :key="`${item.name}-${index}`"><td><strong>{{ item.name ?? `积分包 ${index + 1}` }}</strong></td><td>{{ formatPackageAmount(item.total ?? item.cap) }}<small>{{ item.unit ?? "credits" }}</small></td><td>{{ formatPackageAmount(item.used) }}</td><td>{{ formatPackageAmount(item.remaining ?? item.available) }}</td><td>{{ formatExpiry(item.expires_at) ?? "--" }}</td></tr></tbody></table></div>
          <div v-if="detailPackages.length" class="list-pagination"><span>第 {{ detailPage }} / {{ detailPageCount }} 页 · 共 {{ detailPackages.length }} 包</span><div><button class="secondary-button compact-button" type="button" :disabled="detailPage <= 1" @click="setDetailPage(-1)">上一页</button><button class="secondary-button compact-button" type="button" :disabled="detailPage >= detailPageCount" @click="setDetailPage(1)">下一页</button></div></div>
        </div>
      </div>
    </div>

    <OperationStatus :operation="lastOperation" />
    <NotificationRegion :notifications="notifications" @dismiss="dismiss" />
  </section>
</template>

<style scoped>
.refresh-control { display: inline-flex; align-items: center; gap: 7px; color: var(--muted); font-size: var(--text-xs); }
.refresh-control select { min-height: var(--control-h); width: 100px; }
.summary-date { font-size: 16px; }
.value-positive { color: var(--ok) !important; }
.value-negative { color: var(--err) !important; }
.credits-table td { vertical-align: middle; }
.credits-table td small { display: block; margin-top: 4px; color: var(--faint); font-size: var(--text-xs); }
.credits-table tr { cursor: pointer; }
.credit-value { color: var(--text); font-family: var(--mono); font-variant-numeric: tabular-nums; }
.credits-detail-modal { position: fixed; inset: 0; z-index: 100; display: grid; place-items: center; }
.credits-detail-backdrop { position: absolute; inset: 0; background: rgb(0 0 0 / 0.6); }
.credits-detail-dialog { position: relative; width: min(720px, 92vw); max-height: 80vh; display: flex; flex-direction: column; border: 1px solid var(--line-strong); border-radius: var(--radius); background: var(--surface); box-shadow: var(--overlay); }
.credits-detail-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 18px; border-bottom: 1px solid var(--line); background: var(--surface-raised); }
.credits-detail-header h2 { margin: 0; font-size: var(--text-md); font-weight: 600; }
.credits-detail-body { flex: 1; overflow-y: auto; padding: 12px 18px 14px; }
.credits-detail-table { min-width: 520px; }
.credits-detail-table th, .credits-detail-table td { padding: 8px 12px; font-size: 11px; white-space: nowrap; }
.credits-detail-table th { color: var(--muted); font-size: 10px; font-weight: 600; letter-spacing: .02em; }
.credits-detail-table td strong { color: var(--text); font-size: 12px; font-weight: 600; }
.credits-detail-table td small { display: block; margin-top: 2px; color: var(--faint); font-size: 10px; }
@media (max-width: 760px) { .refresh-control { width: 100%; justify-content: space-between; } .refresh-control select { flex: 1; } }
</style>
