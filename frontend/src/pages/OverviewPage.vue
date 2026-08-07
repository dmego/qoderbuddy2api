<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { Activity, CheckCircle2, Coins, Cpu, RefreshCcw, TriangleAlert, UsersRound } from "@lucide/vue";
import { computed } from "vue";

import MetricChart from "@/components/MetricChart.vue";
import PanelHeader from "@/components/PanelHeader.vue";
import StatePill from "@/components/StatePill.vue";
import { apiRequest } from "@/api/client";
import { formatTokens } from "@/utils/format";
import { statusLabel } from "@/utils/presentation";

type Account = { provider: string; enabled: boolean; summary_status: string };
type Model = { enabled: boolean };
type Summary = { request_count: number; input_tokens: number; output_tokens: number; token_event_count: number; error_count: number };
type Metric = { provider: string; account_id: string; metric_kind: string; status: string; value: unknown };
type Rollup = { bucket_start: string; request_count: number };
const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => apiRequest<{ accounts: Account[] }>("/accounts") });
const models = useQuery({ queryKey: ["models"], queryFn: () => apiRequest<{ models: Model[] }>("/models") });
const usage = useQuery({ queryKey: ["usage-summary"], queryFn: () => apiRequest<{ summary: Summary }>("/usage/summary"), refetchInterval: 10000 });
const metrics = useQuery({ queryKey: ["metrics"], queryFn: () => apiRequest<{ snapshots: Metric[] }>("/metrics/accounts"), refetchInterval: 30000 });
const service = useQuery({ queryKey: ["service"], queryFn: () => apiRequest<{ observed_state: string; in_flight: number }>("/service"), refetchInterval: 3000 });
const checkin = useQuery({ queryKey: ["checkin-status"], queryFn: () => apiRequest<{ enabled: boolean; daily_states: { terminal_outcome?: string }[] }>("/checkin/status") });
const rollups = useQuery({ queryKey: ["usage-rollups", "minute"], queryFn: () => apiRequest<{ rollups: Rollup[] }>("/usage/rollups?bucket_kind=minute&limit=24") });
const chart = computed(() => { const rows = [...(rollups.data.value?.rollups ?? [])].reverse(); return { labels: rows.map((row) => row.bucket_start.slice(11, 16)), values: rows.map((row) => row.request_count) }; });
const alerts = computed(() => { const accountIssues = (accounts.data.value?.accounts ?? []).filter((item) => item.summary_status === "action_required").length; const stale = (metrics.data.value?.snapshots ?? []).filter((item) => item.status === "stale" || item.status === "unavailable").length; return accountIssues + stale + (service.data.value?.observed_state === "FAILED" ? 1 : 0); });
const summary = computed(() => [
  { label: "可用账号", value: String((accounts.data.value?.accounts ?? []).filter((item) => item.enabled).length), note: "CodeBuddy / Qoder", icon: UsersRound },
  { label: "启用模型", value: String((models.data.value?.models ?? []).filter((item) => item.enabled).length), note: statusLabel(service.data.value?.observed_state ?? "代理服务未连接"), icon: Cpu },
  { label: "请求总数", value: String(usage.data.value?.summary?.request_count ?? 0), note: `错误 ${usage.data.value?.summary?.error_count ?? 0}`, icon: Activity },
  { label: "Token", value: usage.data.value?.summary?.token_event_count ? formatTokens((usage.data.value.summary.input_tokens ?? 0) + (usage.data.value.summary.output_tokens ?? 0)) : "不可用", note: "仅统计实际用量事件", icon: Coins },
]);
async function refreshAll(): Promise<void> { await Promise.all([accounts.refetch(), models.refetch(), usage.refetch(), metrics.refetch(), service.refetch(), checkin.refetch(), rollups.refetch()]); }
</script>

<template>
  <section class="page-content">
    <header class="page-header"><div><h1>运行总览</h1><p>代理、账号、模型、Token、积分和签到的统一运行视图。</p></div><button class="secondary-button" type="button" @click="refreshAll"><RefreshCcw :size="16" />刷新全部</button></header>
    <div class="summary-grid" aria-label="关键指标"><article v-for="item in summary" :key="item.label" class="summary-tile"><component :is="item.icon" :size="18" /><span>{{ item.label }}</span><strong>{{ item.value }}</strong><small>{{ item.note }}</small></article></div>
    <div class="overview-grid">
      <section class="data-panel data-panel--wide"><PanelHeader title="请求趋势" description="最近 24 个分钟聚合"><template #default><StatePill :value="service.data.value?.observed_state" /></template></PanelHeader><MetricChart v-if="chart.values.length" :labels="chart.labels" :values="chart.values" /><div v-else class="chart-empty"><Activity :size="22" /><span>代理服务产生遥测事件后显示趋势</span></div></section>
      <section class="data-panel"><PanelHeader title="账号池状态" description="可用账号与异常状态" /><ul class="health-list"><li><span>CodeBuddy</span><strong>{{ accounts.data.value?.accounts?.filter((item) => item.provider === 'codebuddy' && item.enabled).length ?? 0 }} 可用</strong></li><li><span>Qoder</span><strong>{{ accounts.data.value?.accounts?.filter((item) => item.provider === 'qoder' && item.enabled).length ?? 0 }} 可用</strong></li></ul></section>
      <section class="data-panel"><PanelHeader title="签到状态" description="今日批次"><template #default><CheckCircle2 :size="17" /></template></PanelHeader><div class="large-state"><strong>{{ checkin.data.value?.daily_states?.filter((item) => item.terminal_outcome).length ?? 0 }}</strong><span>个账号今日已完成</span><StatePill :value="checkin.data.value?.enabled ? 'enabled' : 'disabled'" /></div></section>
      <section class="data-panel data-panel--wide"><PanelHeader title="需要处理" description="认证、配额、遥测和服务异常"><template #default><TriangleAlert :size="17" /></template></PanelHeader><div v-if="alerts === 0" class="compact-empty">当前没有已确认的运行告警</div><div v-else class="alert-count"><strong>{{ alerts }}</strong><span>项需要检查</span></div></section>
    </div>
  </section>
</template>
