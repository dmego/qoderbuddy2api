<script setup lang="ts">
import { useMutation, useQuery } from "@tanstack/vue-query";
import { Activity, BarChart3, CalendarClock, Download, RefreshCcw, X } from "@lucide/vue";
import { computed, ref, watch } from "vue";

import { apiRequest } from "@/api/client";
import MetricChart from "@/components/MetricChart.vue";
import NotificationRegion from "@/components/NotificationRegion.vue";
import OperationStatus from "@/components/OperationStatus.vue";
import PaginatedTable from "@/components/PaginatedTable.vue";
import PanelHeader from "@/components/PanelHeader.vue";
import StatePill from "@/components/StatePill.vue";
import { appendQuery, useCursorPager } from "@/composables/useCursorPager";
import { useNotifications } from "@/composables/useNotifications";

type Summary = { request_count: number; input_tokens: number; output_tokens: number; success_count: number; error_count: number; token_event_count: number; missing_token_count: number; observed_at?: string; status?: string };
type Rollup = { bucket_start: string; bucket_kind: string; request_count: number; input_tokens: number; output_tokens: number; token_event_count: number; missing_token_count: number };
type UsageEvent = { event_id: string; request_id: string; provider: string; account_id: string | null; model_id: string; protocol: string; status: string; http_status: number | null; input_tokens: number | null; output_tokens: number | null; latency_ms: number | null; stream_committed: boolean | null; started_at: string; finished_at?: string | null; error_code?: string | null };
type EventPage = { events: UsageEvent[]; next_cursor?: string | null; total?: number };

const range = ref("minute");
const provider = ref("");
const accountId = ref("");
const modelId = ref("");
const statusFilter = ref("");
const startedAfter = ref("");
const startedBefore = ref("");
const selectedEventId = ref("");
const lastOperation = ref<Record<string, unknown> | null>(null);
const { cursor, page, canPrevious, next, previous, reset } = useCursorPager();
const { notifications, notify, dismiss } = useNotifications();

const filterQuery = computed(() => {
  const params = new URLSearchParams();
  addFilter(params, "provider", provider.value); addFilter(params, "account_id", accountId.value);
  addFilter(params, "model_id", modelId.value); addFilter(params, "status", statusFilter.value);
  addFilter(params, "started_after", startedAfter.value); addFilter(params, "started_before", startedBefore.value);
  return params.toString();
});
watch(filterQuery, () => { reset(); selectedEventId.value = ""; });

const summary = useQuery({ queryKey: ["usage-summary", filterQuery], queryFn: () => apiRequest<{ summary: Summary }>(withFilters("/usage/summary")), refetchInterval: 10000, staleTime: 15_000 });
const timeseries = useQuery({ queryKey: ["usage-timeseries", range, filterQuery], queryFn: () => apiRequest<{ rollups: Rollup[] }>(withFilters(`/usage/timeseries?bucket_kind=${range.value}&limit=60`)), staleTime: 30_000 });
const events = useQuery({ queryKey: ["usage-events", cursor, filterQuery], queryFn: () => apiRequest<EventPage>(appendQuery(withFilters("/usage/events"), { limit: 25, cursor: cursor.value })), staleTime: 15_000 });
const detail = useQuery({ queryKey: ["usage-event", selectedEventId], queryFn: () => apiRequest<UsageEvent>(`/usage/events/${encodeURIComponent(selectedEventId.value)}`), enabled: computed(() => Boolean(selectedEventId.value)) });
const refresh = useMutation({
  mutationFn: () => apiRequest<Record<string, unknown>>("/usage/rollup", { method: "POST" }),
  onSuccess: async (result) => { lastOperation.value = { action: "重算用量聚合", status: "succeeded", ...result }; notify("用量聚合已更新", { tone: "success" }); await refreshData(); },
  onError: (error) => notify("聚合更新失败", { message: String(error), tone: "error" }),
});
const exporter = useMutation({
  mutationFn: exportCsv,
  onSuccess: () => notify("用量 CSV 已导出", { message: "筛选条件已写入导出请求。", tone: "success" }),
  onError: (error) => notify("用量导出失败", { message: String(error), tone: "error" }),
});

const chart = computed(() => { const values = [...(timeseries.data.value?.rollups ?? [])].reverse(); return { labels: values.map((item) => bucketLabel(item.bucket_start, range.value)), requests: values.map((item) => item.request_count) }; });
const exportHref = computed(() => withFilters("/usage/export?limit=500"));
const partialUnavailable = computed(() => [summary, timeseries, events].some((query) => query.isError.value));

function addFilter(params: URLSearchParams, key: string, value: string): void { if (value.trim()) params.set(key, value.trim()); }
function withFilters(path: string): string { const separator = path.includes("?") ? "&" : "?"; return filterQuery.value ? `${path}${separator}${filterQuery.value}` : path; }
function bucketLabel(value: string, kind: string): string { if (kind === "month") return value.slice(0, 7); if (kind === "day") return value.slice(5, 10); return value.slice(11, 16); }
function tokenLabel(event: UsageEvent): string { return event.input_tokens == null && event.output_tokens == null ? "不可用" : `${event.input_tokens ?? 0} + ${event.output_tokens ?? 0}`; }
function number(value?: number): string { return value === undefined ? "--" : value.toLocaleString(); }
function resetFilters(): void { provider.value = ""; accountId.value = ""; modelId.value = ""; statusFilter.value = ""; startedAfter.value = ""; startedBefore.value = ""; }
async function refreshData(): Promise<void> { await Promise.all([summary.refetch(), timeseries.refetch(), events.refetch()]); }
async function exportCsv(): Promise<void> {
  const response = await fetch(`/api/admin${exportHref.value}`, { credentials: "same-origin", headers: { Accept: "text/csv" } });
  if (!response.ok) throw new Error(`导出请求失败 (${response.status})`);
  const objectUrl = URL.createObjectURL(await response.blob());
  const link = document.createElement("a"); link.href = objectUrl; link.download = "usage-events.csv"; link.click();
  URL.revokeObjectURL(objectUrl);
}
</script>

<template>
  <section class="page-content">
    <header class="page-header"><div><p class="eyebrow">Request telemetry</p><h1>用量监控</h1><p>让摘要、趋势、事件明细和导出共享同一组筛选边界。</p></div><div class="header-actions"><select v-model="range" aria-label="聚合粒度"><option value="minute">分钟</option><option value="day">天</option><option value="month">月</option></select><a class="secondary-button" :href="exportHref" download="usage-events.csv" :aria-disabled="exporter.isPending.value" @click.prevent="exporter.mutate()"><Download :size="16" />{{ exporter.isPending.value ? "正在导出" : "导出 CSV" }}</a><button class="secondary-button" type="button" :disabled="summary.isFetching.value" @click="refreshData"><RefreshCcw :class="{ spin: summary.isFetching.value }" :size="16" />刷新</button><button type="button" :disabled="refresh.isPending.value" @click="refresh.mutate()"><BarChart3 :size="16" />重算聚合</button></div></header>

    <section class="data-panel usage-filters"><PanelHeader title="筛选请求" description="所有条件同时作用于摘要、趋势、事件详情和 CSV 导出。"><button class="secondary-button compact-button" type="button" @click="resetFilters"><X :size="14" />清除</button></PanelHeader><div class="usage-filter-grid"><label>Provider<select v-model="provider" aria-label="Provider"><option value="">全部 Provider</option><option value="codebuddy">CodeBuddy</option><option value="qoder">Qoder</option><option value="workbuddy">WorkBuddy</option></select></label><label>账号 ID<input v-model.trim="accountId" aria-label="账号 ID" placeholder="例如 qd-1" /></label><label>模型 ID<input v-model.trim="modelId" aria-label="模型 ID" placeholder="例如 model-a" /></label><label>状态<select v-model="statusFilter" aria-label="请求状态"><option value="">全部</option><option value="succeeded">成功</option><option value="failed">失败</option></select></label><label>开始时间<input v-model="startedAfter" aria-label="开始时间" type="datetime-local" /></label><label>结束时间<input v-model="startedBefore" aria-label="结束时间" type="datetime-local" /></label></div></section>

    <div v-if="partialUnavailable" class="data-state data-state--warning" role="status">部分遥测数据暂不可用；页面会保留可读取的摘要、趋势或事件，避免整页空白。</div>
    <div class="summary-grid summary-grid--five" :aria-busy="summary.isPending.value"><article class="summary-tile"><Activity :size="18" /><span>请求总数</span><strong>{{ number(summary.data.value?.summary.request_count) }}</strong><small>成功 {{ number(summary.data.value?.summary.success_count) }}</small></article><article class="summary-tile"><BarChart3 :size="18" /><span>输入 Token</span><strong>{{ summary.data.value?.summary.token_event_count ? number(summary.data.value.summary.input_tokens) : "不可用" }}</strong><small>真实 usage 事件</small></article><article class="summary-tile"><BarChart3 :size="18" /><span>输出 Token</span><strong>{{ summary.data.value?.summary.token_event_count ? number(summary.data.value.summary.output_tokens) : "不可用" }}</strong><small>缺失 {{ number(summary.data.value?.summary.missing_token_count) }} 个事件</small></article><article class="summary-tile"><CalendarClock :size="18" /><span>错误率</span><strong>{{ summary.data.value ? `${Math.round(summary.data.value.summary.error_count / Math.max(summary.data.value.summary.request_count, 1) * 100)}%` : "--" }}</strong><small><StatePill :value="summary.data.value?.summary.status ?? (summary.isStale.value ? 'stale' : 'fresh')" /></small></article></div>

    <section class="data-panel"><PanelHeader title="请求趋势" :description="`${range} 聚合 · 最近 60 个桶`" /><div v-if="timeseries.isPending.value" class="loading-row">正在读取趋势…</div><div v-else-if="timeseries.isError.value" class="data-state data-state--error">趋势读取失败。<button class="secondary-button compact-button" type="button" @click="timeseries.refetch()">重试</button></div><div v-else-if="!chart.labels.length" class="compact-empty">当前筛选范围暂无聚合数据。</div><MetricChart v-else :labels="chart.labels" :values="chart.requests" /></section>

    <section class="data-panel"><PanelHeader title="请求事件" :description="`第 ${page} 页 · 详情仅含脱敏状态元数据`" /><PaginatedTable aria-label="请求事件" :loading="events.isPending.value" :error="events.isError.value ? `事件读取失败：${events.error.value}` : ''" :empty="!(events.data.value?.events.length)" empty-title="暂无匹配事件" empty-description="放宽筛选时间或检查 Worker 是否已产生请求。" :stale="events.isStale.value" :page="page" :total="events.data.value?.total" :page-size="25" :can-previous="canPrevious.length > 0" :can-next="Boolean(events.data.value?.next_cursor)" @retry="events.refetch()" @previous="previous" @next="next(events.data.value?.next_cursor)"><template #header><tr><th>时间</th><th>Provider / 账号</th><th>模型</th><th>协议</th><th>状态</th><th>Token</th><th>延迟</th></tr></template><tr v-for="event in events.data.value?.events ?? []" :key="event.event_id" :class="{ selected: selectedEventId === event.event_id }"><td><button class="table-link" :data-testid="`usage-event-${event.event_id}`" type="button" @click="selectedEventId = event.event_id"><strong>{{ event.started_at }}</strong><small>{{ event.event_id }}</small></button></td><td>{{ event.provider }}<small>{{ event.account_id ?? "账号未知" }}</small></td><td class="mono">{{ event.model_id }}</td><td>{{ event.protocol }}</td><td><StatePill :value="event.status" /><small>HTTP {{ event.http_status ?? "--" }}</small></td><td>{{ tokenLabel(event) }}</td><td>{{ event.latency_ms == null ? "--" : `${event.latency_ms} ms` }}</td></tr></PaginatedTable></section>

    <OperationStatus :operation="lastOperation" />

    <aside v-if="selectedEventId" class="detail-drawer data-panel" aria-label="事件详情"><div class="drawer-heading"><div><p class="eyebrow">Safe event detail</p><h2>事件详情</h2><p class="mono">{{ selectedEventId }}</p></div><button class="icon-button" type="button" aria-label="关闭事件详情" @click="selectedEventId = ''"><X :size="16" /></button></div><div v-if="detail.isPending.value" class="loading-row">正在读取详情…</div><div v-else-if="detail.isError.value" class="data-state data-state--error">事件详情读取失败。<button class="secondary-button compact-button" type="button" @click="detail.refetch()">重试</button></div><dl v-else-if="detail.data.value" class="detail-list"><div><dt>请求 ID</dt><dd class="mono">{{ detail.data.value.request_id }}</dd></div><div><dt>Provider / 账号</dt><dd>{{ detail.data.value.provider }} / {{ detail.data.value.account_id ?? "--" }}</dd></div><div><dt>模型 / 协议</dt><dd>{{ detail.data.value.model_id }} / {{ detail.data.value.protocol }}</dd></div><div><dt>状态</dt><dd><StatePill :value="detail.data.value.status" /> HTTP {{ detail.data.value.http_status ?? "--" }}</dd></div><div><dt>Token</dt><dd>{{ tokenLabel(detail.data.value) }}</dd></div><div><dt>流式提交</dt><dd>{{ detail.data.value.stream_committed ? "已提交首块" : "未提交首块" }}</dd></div><div><dt>错误代码</dt><dd>{{ detail.data.value.error_code ?? "--" }}</dd></div></dl></aside>
    <NotificationRegion :notifications="notifications" @dismiss="dismiss" />
  </section>
</template>
