<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { ChevronRight, Filter, Plus, RefreshCcw, ShieldCheck, Trash2, UserRound } from "@lucide/vue";
import { computed, ref } from "vue";

import AccountImportPanel from "@/components/AccountImportPanel.vue";
import PanelHeader from "@/components/PanelHeader.vue";
import StatePill from "@/components/StatePill.vue";
import { apiRequest } from "@/api/client";

type Account = { provider: string; account_id: string; label: string; source: string; enabled: boolean; summary_status: string; masked_identity?: string; shadowed?: boolean; purposes: Record<string, { enabled: boolean; status: string; verification_status: string }> };
type Metric = { provider: string; account_id: string; metric_kind: string; status: string; observed_at?: string; value: Record<string, unknown> | null };
const queryClient = useQueryClient();
const search = ref("");
const provider = ref("all");
const showImport = ref(false);
const selected = ref<Account | null>(null);
const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => apiRequest<{ accounts: Account[] }>("/accounts") });
const metrics = useQuery({ queryKey: ["metrics"], queryFn: () => apiRequest<{ snapshots: Metric[] }>("/metrics/accounts") });
const filtered = computed(() => (accounts.data.value?.accounts ?? []).filter((item) => (provider.value === "all" || item.provider === provider.value) && `${item.label} ${item.account_id}`.toLowerCase().includes(search.value.toLowerCase())));
const selectedMetrics = computed(() => (metrics.data.value?.snapshots ?? []).filter((item) => item.provider === selected.value?.provider && item.account_id === selected.value?.account_id));
const action = useMutation({
  mutationFn: async ({ account, kind }: { account: Account; kind: string }) => {
    if (kind === "delete") return apiRequest(`/accounts/${account.provider}/${account.account_id}`, { method: "DELETE" });
    if (kind === "promote") return apiRequest(`/accounts/${account.provider}/${account.account_id}/promote`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ label: account.label }) });
    const next = !account.enabled;
    return apiRequest(`/accounts/${account.provider}/${account.account_id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: next, label: account.label }) });
  },
  onSuccess: async () => { selected.value = null; await queryClient.invalidateQueries({ queryKey: ["accounts"] }); },
});

function run(account: Account, kind: string): void {
  if (kind === "delete" && !window.confirm(`确认删除 ${account.label}？这会撤销其代理与签到凭据。`)) return;
  action.mutate({ account, kind });
}

function metricFor(account: Account): string {
  const rows = metrics.data.value?.snapshots ?? [];
  const values = rows.filter((row) => row.provider === account.provider && row.account_id === account.account_id);
  return values.some((row) => row.status === "stale") ? "数据过期" : values.length ? "已监控" : "待采集";
}

function metricLabel(kind: string): string {
  const labels: Record<string, string> = { quota: "配额", points: "积分", checkin: "签到", "token:chat": "代理 Token", "token:checkin": "签到 Token" };
  return labels[kind] ?? kind;
}

function metricSummary(metric: Metric): string {
  if (metric.metric_kind === "points" && metric.status === "unknown") return "接口协议尚未验证";
  if (metric.metric_kind === "quota") return quotaSummary(metric.value);
  if (metric.metric_kind.startsWith("token:")) return `凭据${String(metric.value?.status ?? metric.status)}`;
  if (metric.metric_kind === "checkin") return metric.value?.terminal_outcome ? `今日 ${metric.value.terminal_outcome}` : "今日尚未执行";
  return metric.status === "unavailable" ? "暂不可用" : "等待采集";
}

function quotaSummary(value: Record<string, unknown> | null): string {
  const percent = value?.total_usage_percentage;
  return typeof percent === "number" ? `已使用 ${percent}%` : "已获取配额状态";
}
</script>

<template>
  <section class="page-content">
    <header class="page-header"><div><p class="eyebrow">Identity & access</p><h1>账号管理</h1><p>统一维护 CodeBuddy OAuth、Qoder PAT、签到凭据和账号用途。</p></div><button type="button" @click="showImport = !showImport"><Plus :size="16" />添加账号</button></header>
    <AccountImportPanel v-if="showImport" @saved="queryClient.invalidateQueries({ queryKey: ['accounts'] })" />
    <section class="data-panel">
      <PanelHeader title="账号池" :description="`${filtered.length} 个账号 · 环境变量账号只读，持久账号可编辑`"><template #default><div class="toolbar"><div class="input-with-icon compact"><Filter :size="15" /><input v-model="search" placeholder="搜索名称或账号 ID" /></div><select v-model="provider" aria-label="筛选 Provider"><option value="all">全部 Provider</option><option value="codebuddy">CodeBuddy</option><option value="qoder">Qoder</option></select></div></template></PanelHeader>
      <div v-if="accounts.isPending.value" class="loading-row">正在加载账号快照…</div>
      <div v-else-if="accounts.isError.value" class="alert alert--error">账号读取失败：{{ accounts.error.value }}</div>
      <div v-else-if="!filtered.length" class="empty-state"><UserRound :size="28" /><strong>还没有匹配的账号</strong><span>通过上方添加账号，或检查搜索条件。</span></div>
      <div v-else class="table-wrap"><table><thead><tr><th>账号</th><th>Provider</th><th>用途</th><th>来源</th><th>监控</th><th>状态</th><th></th></tr></thead><tbody><tr v-for="account in filtered" :key="`${account.provider}:${account.account_id}`" :class="{ selected: selected?.account_id === account.account_id }"><td><button class="table-link" type="button" @click="selected = account"><strong>{{ account.label }}</strong><small>{{ account.account_id }} · {{ account.masked_identity ?? "无身份掩码" }}</small></button></td><td><span class="provider-mark" :class="`provider-mark--${account.provider}`">{{ account.provider === 'codebuddy' ? 'CB' : 'QD' }}</span></td><td><div class="purpose-list"><StatePill v-for="(purpose, name) in account.purposes" :key="name" :value="purpose.status" /><span v-if="!Object.keys(account.purposes).length">未配置</span></div></td><td>{{ account.source }}<span v-if="account.shadowed" class="text-warning"> · 被覆盖</span></td><td>{{ metricFor(account) }}</td><td><StatePill :value="account.enabled ? account.summary_status : 'disabled'" /></td><td><div class="row-actions"><button class="icon-button" title="启用/停用" type="button" @click="run(account, 'toggle')"><ShieldCheck :size="16" /></button><button v-if="account.source === 'env'" class="icon-button" title="提升为持久账号" type="button" @click="run(account, 'promote')"><ChevronRight :size="16" /></button><button v-else class="icon-button danger-icon" title="删除账号" type="button" @click="run(account, 'delete')"><Trash2 :size="16" /></button></div></td></tr></tbody></table></div>
    </section>
    <aside v-if="selected" class="detail-drawer data-panel"><div class="drawer-heading"><div><p class="eyebrow">Account detail</p><h2>{{ selected.label }}</h2><p>{{ selected.provider }} / {{ selected.account_id }}</p></div><button class="icon-button" type="button" aria-label="关闭详情" @click="selected = null">×</button></div><dl class="detail-list"><div><dt>来源</dt><dd>{{ selected.source }}</dd></div><div><dt>身份掩码</dt><dd>{{ selected.masked_identity ?? "--" }}</dd></div><div><dt>总体状态</dt><dd><StatePill :value="selected.summary_status" /></dd></div></dl><h3>用途状态</h3><div class="purpose-cards"><div v-for="(purpose, name) in selected.purposes" :key="name"><strong>{{ name }}</strong><StatePill :value="purpose.status" /><small>{{ purpose.verification_status }}</small></div></div><div class="drawer-subhead"><h3>Token、配额与积分</h3><button class="icon-button" type="button" title="刷新账号指标" @click="metrics.refetch()"><RefreshCcw :size="15" /></button></div><div v-if="selectedMetrics.length" class="metric-list"><div v-for="metric in selectedMetrics" :key="metric.metric_kind"><strong>{{ metricLabel(metric.metric_kind) }}</strong><StatePill :value="metric.status" /><span>{{ metricSummary(metric) }}</span><small>采集于 {{ metric.observed_at ?? "--" }}</small></div></div><p v-else class="compact-empty">尚未采集账号指标</p><p v-if="action.isError.value" class="form-error">{{ action.error.value }}</p></aside>
  </section>
</template>
