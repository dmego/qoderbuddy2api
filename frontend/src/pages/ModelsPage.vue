<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { Activity, Boxes, Check, Filter, RefreshCcw, Search, X } from "@lucide/vue";
import { computed, ref } from "vue";

import { apiRequest } from "@/api/client";
import ConfirmDialog from "@/components/ConfirmDialog.vue";
import NotificationRegion from "@/components/NotificationRegion.vue";
import OperationStatus from "@/components/OperationStatus.vue";
import PaginatedTable from "@/components/PaginatedTable.vue";
import PanelHeader from "@/components/PanelHeader.vue";
import StatePill from "@/components/StatePill.vue";
import { appendQuery, useCursorPager } from "@/composables/useCursorPager";
import { useNotifications } from "@/composables/useNotifications";

type Model = {
  provider: string; model_id: string; display_name: string; capabilities: string[];
  source: string; enabled: boolean; last_seen_at?: string; description?: string;
};
type ModelPage = { models: Model[]; next_cursor?: string | null; total?: number };
type UsageSummary = { request_count: number; success_count: number; error_count: number; avg_latency_ms?: number; p95_latency_ms?: number };

const queryClient = useQueryClient();
const draftSearch = ref("");
const search = ref("");
const provider = ref("");
const source = ref("");
const enabled = ref("");
const capability = ref("");
const selected = ref<Model | null>(null);
const pendingDisable = ref<Model | null>(null);
const lastOperation = ref<Record<string, unknown> | null>(null);
const { cursor, page, canPrevious, next, previous, reset } = useCursorPager();
const { notifications, notify, dismiss } = useNotifications();

const models = useQuery({
  queryKey: ["models", cursor, search, provider, source, enabled, capability],
  queryFn: () => apiRequest<ModelPage>(appendQuery("/models", { limit: 20, cursor: cursor.value, search: search.value, provider: provider.value, source: source.value, enabled: enabled.value, capability: capability.value })),
  staleTime: 30_000,
});
const modelUsage = useQuery({
  queryKey: ["model-usage", computed(() => selected.value?.provider), computed(() => selected.value?.model_id)],
  enabled: computed(() => Boolean(selected.value)),
  queryFn: () => apiRequest<{ summary: UsageSummary }>(appendQuery("/usage/summary", { provider: selected.value?.provider, model_id: selected.value?.model_id })),
  staleTime: 30_000,
});
const refresh = useMutation({
  mutationFn: () => apiRequest<Record<string, unknown>>("/models/refresh", { method: "POST" }),
  onSuccess: async (result) => { lastOperation.value = { action: "刷新模型目录", ...result }; notify("模型目录已刷新", { message: `${result.refreshed ?? "目录"} 个定义已处理`, tone: "success" }); await queryClient.invalidateQueries({ queryKey: ["models"] }); },
  onError: (error) => notify("模型目录刷新失败", { message: String(error), tone: "error" }),
});
const toggle = useMutation({
  mutationFn: (model: Model) => apiRequest<Record<string, unknown>>(`/models/${encodeURIComponent(model.provider)}/${encodeURIComponent(model.model_id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: !model.enabled }) }),
  onSuccess: async (result, model) => { lastOperation.value = { action: `${model.enabled ? "停用" : "启用"}模型`, status: "succeeded", model_id: model.model_id, ...result }; notify(`模型已${model.enabled ? "停用" : "启用"}`, { message: model.model_id, tone: "success" }); selected.value = null; await queryClient.invalidateQueries({ queryKey: ["models"] }); },
  onError: (error) => notify("模型状态更新失败", { message: String(error), tone: "error" }),
});
const probe = useMutation({
  mutationFn: (model: Model) => apiRequest<Record<string, unknown>>(`/models/${encodeURIComponent(model.provider)}/${encodeURIComponent(model.model_id)}/probe`, { method: "POST" }),
  onSuccess: (result, model) => { lastOperation.value = { action: "模型探测", model_id: model.model_id, ...result }; notify(result.status === "succeeded" ? "模型探测成功" : "模型探测已返回", { message: result.latency_ms ? `${result.latency_ms} ms` : model.model_id, tone: result.status === "failed" ? "warning" : "success" }); },
  onError: (error) => notify("模型探测失败", { message: String(error), tone: "error" }),
});

function applySearch(): void { search.value = draftSearch.value.trim(); reset(); }
function clearFilters(): void { draftSearch.value = ""; search.value = ""; provider.value = ""; source.value = ""; enabled.value = ""; capability.value = ""; reset(); }
function requestToggle(model: Model): void { if (model.enabled) pendingDisable.value = model; else toggle.mutate(model); }
function errorRate(summary?: UsageSummary): string { if (!summary?.request_count) return "--"; return `${Math.round(summary.error_count / summary.request_count * 100)}%`; }
</script>

<template>
  <section class="page-content">
    <header class="page-header">
      <div><p class="eyebrow">Provider catalogue</p><h1>模型管理</h1><p>控制模型可见性，核对发现来源，并通过固定最小请求验证上游可用性。</p></div>
      <div class="header-actions"><button class="secondary-button" type="button" :disabled="models.isFetching.value" @click="models.refetch()"><RefreshCcw :class="{ spin: models.isFetching.value }" :size="16" />读取目录</button><button type="button" :disabled="refresh.isPending.value" @click="refresh.mutate()"><Boxes :size="16" />刷新目录</button></div>
    </header>

    <section class="data-panel filter-panel">
      <PanelHeader title="目录筛选" description="筛选条件会随分页请求发送，避免在浏览器加载整个目录。"><Filter :size="17" /></PanelHeader>
      <div class="filter-grid filter-grid--six">
        <label class="filter-search">模型名称或 ID<div class="input-with-icon"><Search :size="15" /><input v-model="draftSearch" placeholder="输入后回车搜索" @keyup.enter="applySearch" /></div></label>
        <label>Provider<select v-model="provider" @change="reset"><option value="">全部</option><option value="codebuddy">CodeBuddy</option><option value="qoder">Qoder</option></select></label>
        <label>来源<select v-model="source" @change="reset"><option value="">全部</option><option value="definition">定义文件</option><option value="discovered">上游发现</option></select></label>
        <label>状态<select v-model="enabled" @change="reset"><option value="">全部</option><option value="true">启用</option><option value="false">停用</option></select></label>
        <label>能力<select v-model="capability" @change="reset"><option value="">全部</option><option value="chat">Chat</option><option value="streaming">Streaming</option><option value="tools">Tools</option></select></label>
        <div class="filter-actions"><button type="button" @click="applySearch"><Search :size="15" />应用</button><button class="secondary-button" type="button" @click="clearFilters"><X :size="15" />清除</button></div>
      </div>
    </section>

    <section class="data-panel">
      <PanelHeader title="模型目录" :description="`第 ${page} 页 · 所有变更写入审计`"><StatePill :value="models.isFetching.value ? 'running' : 'active'" /></PanelHeader>
      <PaginatedTable aria-label="模型目录" :loading="models.isPending.value" :error="models.isError.value ? `模型目录读取失败：${models.error.value}` : ''" :empty="!(models.data.value?.models.length)" empty-title="没有匹配的模型" empty-description="调整筛选条件，或刷新模型目录。" :stale="models.isStale.value" :page="page" :total="models.data.value?.total" :can-previous="canPrevious.length > 0" :can-next="Boolean(models.data.value?.next_cursor)" @retry="models.refetch()" @previous="previous" @next="next(models.data.value?.next_cursor)">
        <template #header><tr><th>模型</th><th>Provider</th><th>能力</th><th>来源</th><th>最近发现</th><th>状态</th><th>操作</th></tr></template>
        <tr v-for="model in models.data.value?.models ?? []" :key="`${model.provider}:${model.model_id}`" :class="{ selected: selected?.model_id === model.model_id && selected?.provider === model.provider }"><td><button class="table-link" type="button" @click="selected = model"><strong>{{ model.display_name || model.model_id }}</strong><small class="mono">{{ model.model_id }}</small></button></td><td><span class="provider-mark" :class="`provider-mark--${model.provider}`">{{ model.provider }}</span></td><td><div class="tag-list"><span v-for="item in model.capabilities" :key="item">{{ item }}</span><span v-if="!model.capabilities.length">未声明</span></div></td><td>{{ model.source }}</td><td>{{ model.last_seen_at ?? "--" }}</td><td><StatePill :value="model.enabled" /></td><td><div class="row-actions"><button class="secondary-button compact-button" type="button" :disabled="probe.isPending.value" @click="probe.mutate(model)"><Activity :size="14" />探测</button><button class="icon-button" type="button" :aria-label="model.enabled ? `停用 ${model.model_id}` : `启用 ${model.model_id}`" :disabled="toggle.isPending.value" @click="requestToggle(model)"><Check :size="16" /></button></div></td></tr>
      </PaginatedTable>
    </section>

    <OperationStatus :operation="lastOperation" />

    <aside v-if="selected" class="detail-drawer data-panel" aria-label="模型详情">
      <div class="drawer-heading"><div><p class="eyebrow">Model detail</p><h2>{{ selected.display_name || selected.model_id }}</h2><p class="mono">{{ selected.provider }} / {{ selected.model_id }}</p></div><button class="icon-button" type="button" aria-label="关闭模型详情" @click="selected = null"><X :size="16" /></button></div>
      <dl class="detail-list"><div><dt>状态</dt><dd><StatePill :value="selected.enabled" /></dd></div><div><dt>来源</dt><dd>{{ selected.source }}</dd></div><div><dt>能力</dt><dd>{{ selected.capabilities.join("、") || "未声明" }}</dd></div><div><dt>最近发现</dt><dd>{{ selected.last_seen_at ?? "--" }}</dd></div></dl>
      <h3>当前筛选用量摘要</h3>
      <div v-if="modelUsage.isPending.value" class="loading-row">正在读取用量摘要…</div><div v-else-if="modelUsage.isError.value" class="data-state data-state--warning">用量摘要暂不可用，模型管理操作仍可继续。</div>
      <div v-else class="detail-metrics"><div><span>请求数</span><strong>{{ modelUsage.data.value?.summary.request_count ?? 0 }}</strong></div><div><span>错误率</span><strong>{{ errorRate(modelUsage.data.value?.summary) }}</strong></div><div><span>P95 延迟</span><strong>{{ modelUsage.data.value?.summary.p95_latency_ms ? `${modelUsage.data.value.summary.p95_latency_ms} ms` : "--" }}</strong></div></div>
      <button type="button" :disabled="probe.isPending.value" @click="probe.mutate(selected)"><Activity :size="16" />执行安全探测</button>
    </aside>

    <ConfirmDialog :open="Boolean(pendingDisable)" title="停用这个模型？" :description="`停用 ${pendingDisable?.model_id ?? ''} 后，新请求将无法再选择该模型；历史用量不受影响。`" confirm-label="确认停用" tone="danger" :busy="toggle.isPending.value" @cancel="pendingDisable = null" @confirm="pendingDisable && toggle.mutate(pendingDisable); pendingDisable = null" />
    <NotificationRegion :notifications="notifications" @dismiss="dismiss" />
  </section>
</template>
