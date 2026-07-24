<script setup lang="ts">
import { useMutation, useQuery } from "@tanstack/vue-query";
import { Activity, BarChart3, CalendarClock, Download, RefreshCcw, X } from "@lucide/vue";
import { computed, ref } from "vue";

import { apiRequest } from "@/api/client";
import MetricChart from "@/components/MetricChart.vue";
import PanelHeader from "@/components/PanelHeader.vue";

type Summary = {
  request_count: number;
  input_tokens: number;
  output_tokens: number;
  success_count: number;
  error_count: number;
  token_event_count: number;
  missing_token_count: number;
};

type Rollup = {
  bucket_start: string;
  bucket_kind: string;
  request_count: number;
  input_tokens: number;
  output_tokens: number;
  token_event_count: number;
  missing_token_count: number;
};

type UsageEvent = {
  event_id: string;
  request_id: string;
  provider: string;
  account_id: string | null;
  model_id: string;
  protocol: string;
  status: string;
  http_status: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  latency_ms: number | null;
  stream_committed: boolean | null;
  started_at: string;
  finished_at?: string | null;
  error_code?: string | null;
};

const range = ref("minute");
const provider = ref("");
const accountId = ref("");
const modelId = ref("");
const startedAfter = ref("");
const startedBefore = ref("");
const selectedEventId = ref("");

const filterQuery = computed(() => {
  const params = new URLSearchParams();
  addFilter(params, "provider", provider.value);
  addFilter(params, "account_id", accountId.value);
  addFilter(params, "model_id", modelId.value);
  addFilter(params, "started_after", startedAfter.value);
  addFilter(params, "started_before", startedBefore.value);
  return params.toString();
});

const summary = useQuery({
  queryKey: ["usage-summary", filterQuery],
  queryFn: () => apiRequest<{ summary: Summary }>(withFilters("/usage/summary")),
  refetchInterval: 10000,
});
const timeseries = useQuery({
  queryKey: ["usage-timeseries", range, filterQuery],
  queryFn: () => apiRequest<{ rollups: Rollup[] }>(withFilters(`/usage/timeseries?bucket_kind=${range.value}&limit=60`)),
});
const events = useQuery({
  queryKey: ["usage-events", filterQuery],
  queryFn: () => apiRequest<{ events: UsageEvent[] }>(withFilters("/usage/events?limit=50")),
});
const detail = useQuery({
  queryKey: ["usage-event", selectedEventId],
  queryFn: () => apiRequest<UsageEvent>(`/usage/events/${encodeURIComponent(selectedEventId.value)}`),
  enabled: computed(() => Boolean(selectedEventId.value)),
});
const refresh = useMutation({
  mutationFn: () => apiRequest("/usage/rollup", { method: "POST" }),
  onSuccess: async () => { await refreshData(); },
});

const chart = computed(() => {
  const values = [...(timeseries.data.value?.rollups ?? [])].reverse();
  return {
    labels: values.map((item) => bucketLabel(item.bucket_start, range.value)),
    requests: values.map((item) => item.request_count),
  };
});
const exportHref = computed(() => withFilters("/usage/export?limit=500"));
const errorMessage = computed(() => {
  if (summary.isError.value) return "摘要读取失败，请检查 Control Plane 状态。";
  if (timeseries.isError.value || events.isError.value) return "部分遥测数据读取失败，请重试。";
  return "";
});

function addFilter(params: URLSearchParams, key: string, value: string): void {
  if (value.trim()) params.set(key, value.trim());
}

function withFilters(path: string): string {
  const separator = path.includes("?") ? "&" : "?";
  return filterQuery.value ? `${path}${separator}${filterQuery.value}` : path;
}

function bucketLabel(value: string, kind: string): string {
  if (kind === "month") return value.slice(0, 7);
  if (kind === "day") return value.slice(5, 10);
  return value.slice(11, 16);
}

function tokenLabel(event: UsageEvent): string {
  if (event.input_tokens == null && event.output_tokens == null) return "不可用";
  return `${event.input_tokens ?? 0} + ${event.output_tokens ?? 0}`;
}

function resetFilters(): void {
  provider.value = "";
  accountId.value = "";
  modelId.value = "";
  startedAfter.value = "";
  startedBefore.value = "";
  selectedEventId.value = "";
}

async function refreshData(): Promise<void> {
  await Promise.all([summary.refetch(), timeseries.refetch(), events.refetch()]);
}
</script>

<template>
  <section class="page-content">
    <header class="page-header">
      <div><h1>用量监控</h1><p>按 Provider、账号和模型核对 Worker 请求、Token 与失败边界。</p></div>
      <div class="header-actions">
        <select v-model="range" aria-label="聚合粒度"><option value="minute">分钟</option><option value="day">天</option><option value="month">月</option></select>
        <a class="secondary-button" :href="exportHref" download="usage-events.csv"><Download :size="16" />导出 CSV</a>
        <button class="secondary-button" type="button" :disabled="summary.isFetching.value" @click="refreshData"><RefreshCcw :size="16" :class="{ spin: summary.isFetching.value }" />刷新</button>
        <button type="button" :disabled="refresh.isPending.value" @click="refresh.mutate()"><BarChart3 :size="16" />重算聚合</button>
      </div>
    </header>

    <section class="data-panel usage-filters">
      <PanelHeader title="筛选请求" description="筛选条件同时作用于摘要、趋势、事件和 CSV 导出。">
        <template #default><button class="secondary-button compact-button" type="button" @click="resetFilters"><X :size="14" />清除</button></template>
      </PanelHeader>
      <div class="usage-filter-grid">
        <label>Provider<select v-model="provider" aria-label="Provider"><option value="">全部 Provider</option><option value="codebuddy">CodeBuddy</option><option value="qoder">Qoder</option></select></label>
        <label>账号 ID<input v-model="accountId" aria-label="账号 ID" placeholder="例如 qd-1" /></label>
        <label>模型 ID<input v-model="modelId" aria-label="模型 ID" placeholder="例如 model-a" /></label>
        <label>开始时间<input v-model="startedAfter" aria-label="开始时间" type="datetime-local" /></label>
        <label>结束时间<input v-model="startedBefore" aria-label="结束时间" type="datetime-local" /></label>
      </div>
    </section>

    <p v-if="errorMessage" class="alert" role="alert">{{ errorMessage }}</p>
    <div class="summary-grid summary-grid--five">
      <article class="summary-tile"><Activity :size="18" /><span>请求总数</span><strong>{{ summary.data.value?.summary.request_count ?? "--" }}</strong><small>成功 {{ summary.data.value?.summary.success_count ?? "--" }}</small></article>
      <article class="summary-tile"><BarChart3 :size="18" /><span>输入 Token</span><strong>{{ summary.data.value?.summary.token_event_count ? summary.data.value.summary.input_tokens : "不可用" }}</strong><small>真实 usage 事件</small></article>
      <article class="summary-tile"><BarChart3 :size="18" /><span>输出 Token</span><strong>{{ summary.data.value?.summary.token_event_count ? summary.data.value.summary.output_tokens : "不可用" }}</strong><small>缺失 {{ summary.data.value?.summary.missing_token_count ?? "--" }} 个事件</small></article>
      <article class="summary-tile"><CalendarClock :size="18" /><span>错误率</span><strong>{{ summary.data.value ? `${Math.round(summary.data.value.summary.error_count / Math.max(summary.data.value.summary.request_count, 1) * 100)}%` : "--" }}</strong><small>当前筛选范围</small></article>
    </div>

    <section class="data-panel"><PanelHeader title="请求趋势" :description="`${range} 聚合 · 最近 60 个桶`" /><div v-if="timeseries.isPending.value" class="loading-row">正在读取趋势…</div><div v-else-if="!chart.labels.length" class="compact-empty">当前筛选范围暂无聚合数据。</div><MetricChart v-else :labels="chart.labels" :values="chart.requests" /></section>

    <section class="data-panel"><PanelHeader title="请求事件" description="事件详情只包含状态元数据，不包含请求正文或凭据。" /><div v-if="events.isPending.value" class="loading-row">正在读取事件…</div><div v-else-if="!events.data.value?.events.length" class="compact-empty">暂无匹配事件。</div><div v-else class="table-wrap"><table><thead><tr><th>时间</th><th>Provider / 账号</th><th>模型</th><th>协议</th><th>状态</th><th>Token</th><th>延迟</th></tr></thead><tbody><tr v-for="event in events.data.value.events" :key="event.event_id" :class="{ selected: selectedEventId === event.event_id }"><td><button class="table-link" :data-testid="`usage-event-${event.event_id}`" type="button" @click="selectedEventId = event.event_id"><strong>{{ event.started_at }}</strong><small>{{ event.event_id }}</small></button></td><td>{{ event.provider }}<small>{{ event.account_id ?? "账号未知" }}</small></td><td class="mono">{{ event.model_id }}</td><td>{{ event.protocol }}</td><td>{{ event.status }} / {{ event.http_status ?? "--" }}</td><td>{{ tokenLabel(event) }}</td><td>{{ event.latency_ms == null ? "--" : `${event.latency_ms} ms` }}</td></tr></tbody></table></div></section>

    <aside v-if="selectedEventId" class="detail-drawer data-panel" aria-label="事件详情"><div class="drawer-heading"><div><h2>事件详情</h2><p class="mono">{{ selectedEventId }}</p></div><button class="icon-button" type="button" aria-label="关闭事件详情" @click="selectedEventId = ''"><X :size="16" /></button></div><div v-if="detail.isPending.value" class="loading-row">正在读取详情…</div><dl v-else-if="detail.data.value" class="detail-list"><div><dt>请求 ID</dt><dd class="mono">{{ detail.data.value.request_id }}</dd></div><div><dt>Provider / 账号</dt><dd>{{ detail.data.value.provider }} / {{ detail.data.value.account_id ?? "--" }}</dd></div><div><dt>模型 / 协议</dt><dd>{{ detail.data.value.model_id }} / {{ detail.data.value.protocol }}</dd></div><div><dt>状态</dt><dd>{{ detail.data.value.status }} / {{ detail.data.value.http_status ?? "--" }}</dd></div><div><dt>Token</dt><dd>{{ tokenLabel(detail.data.value) }}</dd></div><div><dt>流式提交</dt><dd>{{ detail.data.value.stream_committed ? "已提交首块" : "未提交首块" }}</dd></div><div><dt>错误代码</dt><dd>{{ detail.data.value.error_code ?? "--" }}</dd></div></dl><p v-else class="alert">事件详情读取失败。</p></aside>
  </section>
</template>
