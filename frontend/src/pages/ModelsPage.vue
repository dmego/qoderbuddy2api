<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { Activity, Boxes, Check, Filter, RefreshCcw, Search, X } from "@lucide/vue";
import { computed, ref } from "vue";

import { apiRequest } from "@/api/client";
import AccessibleDrawer from "@/components/AccessibleDrawer.vue";
import ConfirmDialog from "@/components/ConfirmDialog.vue";
import NotificationRegion from "@/components/NotificationRegion.vue";
import OperationStatus from "@/components/OperationStatus.vue";
import PaginatedTable from "@/components/PaginatedTable.vue";
import PanelHeader from "@/components/PanelHeader.vue";
import StatePill from "@/components/StatePill.vue";
import { appendQuery, useCursorPager } from "@/composables/useCursorPager";
import { useNotifications } from "@/composables/useNotifications";

type ModelRoute = { provider: string; upstream_id: string; enabled: boolean; source: string };
type Model = {
  model_id: string; display_name: string; capabilities: string[];
  source: string; enabled: boolean; last_seen_at?: string; routes: ModelRoute[];
};
type ModelPage = { models: Model[]; next_cursor?: string | null; total?: number };
type ProbeRoute = { provider: string; upstream_id: string; status: string; latency_ms?: number; error_code?: string };
type ProbeResult = { status: string; model_id: string; routes: ProbeRoute[] };
type UsageSummary = { request_count: number; success_count: number; error_count: number; latency_avg_ms?: number | null; latency_p95_ms?: number | null };

const queryClient = useQueryClient();
const draftSearch = ref("");
const search = ref("");
const provider = ref("");
const enabled = ref("");
const capability = ref("");
const selected = ref<Model | null>(null);
const pendingDisable = ref<Model | null>(null);
const lastOperation = ref<Record<string, unknown> | null>(null);
const { cursor, page, canPrevious, next, previous, reset } = useCursorPager();
const { notifications, notify, dismiss } = useNotifications();

const models = useQuery({
  queryKey: ["models", cursor, search, provider, enabled, capability],
  queryFn: () => apiRequest<ModelPage>(appendQuery("/models", { limit: 20, cursor: cursor.value, query: search.value, provider: provider.value, enabled: enabled.value, capability: capability.value })),
  staleTime: 30_000,
});
const modelUsage = useQuery({
  queryKey: ["model-usage", computed(() => selected.value?.model_id)],
  enabled: computed(() => Boolean(selected.value)),
  queryFn: () => apiRequest<{ summary: UsageSummary }>(appendQuery("/usage/summary", { model_id: selected.value?.model_id })),
  staleTime: 30_000,
});
const refresh = useMutation({
  mutationFn: () => apiRequest<Record<string, unknown>>("/models/refresh", { method: "POST" }),
  onSuccess: async (result) => { lastOperation.value = { action: "刷新模型目录", ...result }; notify("模型目录已刷新", { message: `${result.refreshed ?? "目录"} 个定义已处理`, tone: "success" }); await queryClient.invalidateQueries({ queryKey: ["models"] }); },
  onError: (error) => notify("模型目录刷新失败", { message: String(error), tone: "error" }),
});
const syncUpstream = useMutation({
  mutationFn: () => {
    const endpoint = provider.value === "codebuddy" ? "/models/sync/codebuddy" : provider.value === "qoder" ? "/models/sync/qoder" : "/models/sync";
    return apiRequest<{ added: number; updated: number; removed?: number; disabled?: number; probed?: number; providers?: Record<string, { status: string; added?: number }> }>(endpoint, { method: "POST" });
  },
  onSuccess: async (result) => {
    let detail: string;
    if (result.providers) {
      const q = result.providers.qoder;
      const c = result.providers.codebuddy;
      detail = `WorkBuddy ${c ? `新增 ${c.added ?? 0}` : "失败"} · Qoder ${q && q.status === "succeeded" ? `更新 ${q.added ?? 0}` : "失败"}`;
    } else if (provider.value === "codebuddy") {
      detail = `探测 ${result.probed ?? 0} · 新增 ${result.added} · 更新 ${result.updated} · 移除 ${result.removed ?? 0}`;
    } else {
      detail = `新增 ${result.added} · 更新 ${result.updated} · 停用 ${result.disabled ?? 0}`;
    }
    notify("上游同步完成", { message: detail, tone: "success" });
    await queryClient.invalidateQueries({ queryKey: ["models"] });
  },
  onError: (error) => notify("上游同步失败", { message: String(error).includes("401") || String(error).includes("403") ? `${provider.value === "qoder" ? "Qoder" : "WorkBuddy"} 凭据失效，请在账号页检查` : String(error), tone: "error" }),
});

const toggle = useMutation({
  mutationFn: (model: Model) => apiRequest<Record<string, unknown>>(`/models/${encodeURIComponent(model.model_id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: !model.enabled }) }),
  onSuccess: async (result, model) => { lastOperation.value = { action: `${model.enabled ? "停用" : "启用"}模型`, status: "succeeded", ...result }; notify(`模型已${model.enabled ? "停用" : "启用"}`, { message: model.model_id, tone: "success" }); selected.value = null; await queryClient.invalidateQueries({ queryKey: ["models"] }); },
  onError: (error) => notify("模型状态更新失败", { message: String(error), tone: "error" }),
});
const probe = useMutation({
  mutationFn: (model: Model) => apiRequest<ProbeResult>(`/models/${encodeURIComponent(model.model_id)}/probe`, { method: "POST" }),
  onSuccess: (result) => {
    const detail = result.routes.map((route) => route.status === "succeeded" ? `${route.provider} ${route.latency_ms ?? "--"}ms` : `${route.provider} 失败`).join(" · ");
    lastOperation.value = { action: "模型探测", ...result };
    notify(result.status === "succeeded" ? "模型探测成功" : "部分路线探测失败", { message: detail, tone: result.status === "succeeded" ? "success" : "warning" });
  },
  onError: (error) => notify("模型探测失败", { message: String(error), tone: "error" }),
});

function applySearch(): void { search.value = draftSearch.value.trim(); reset(); }
function clearFilters(): void { draftSearch.value = ""; search.value = ""; provider.value = ""; enabled.value = ""; capability.value = ""; reset(); }
function requestToggle(model: Model): void { if (model.enabled) pendingDisable.value = model; else toggle.mutate(model); }
function errorRate(summary?: UsageSummary): string { if (!summary?.request_count) return "--"; return `${Math.round(summary.error_count / summary.request_count * 100)}%`; }
function enabledRouteCount(model: Model): number { return model.routes.filter((route) => route.enabled).length; }
</script>

<template>
  <section class="page-content">
    <header class="page-header">
      <div><h1>模型管理</h1><p>统一模型目录：同一模型跨提供商合并展示，请求内部自动轮询；可分别核对各路线可用性。</p></div>
      <div class="header-actions"><button class="secondary-button" type="button" :disabled="models.isFetching.value" @click="models.refetch()"><RefreshCcw :class="{ spin: models.isFetching.value }" :size="16" />读取目录</button><button type="button" :disabled="refresh.isPending.value" @click="refresh.mutate()"><Boxes :size="16" />刷新目录</button><button type="button" :disabled="syncUpstream.isPending.value || (provider !== '' && provider !== 'qoder' && provider !== 'codebuddy')" aria-label="从上游同步" @click="syncUpstream.mutate()"><RefreshCcw :class="{ spin: syncUpstream.isPending.value }" :size="16" />从上游同步</button></div>
    </header>

    <section class="data-panel filter-panel">
      <PanelHeader title="目录筛选" description="筛选条件会随分页请求发送，避免在浏览器加载整个目录。"><Filter :size="17" /></PanelHeader>
      <div class="filter-grid filter-grid--five">
        <label class="filter-search">模型名称或 ID<div class="input-with-icon"><Search :size="15" /><input v-model="draftSearch" placeholder="输入后回车搜索" @keyup.enter="applySearch" /></div></label>
        <label>包含提供方<select v-model="provider" @change="reset"><option value="">全部</option><option value="codebuddy">CodeBuddy</option><option value="qoder">Qoder</option></select></label>
        <label>来源<select v-model="enabled" @change="reset"><option value="">全部</option><option value="true">启用</option><option value="false">停用</option></select></label>
        <label>能力<select v-model="capability" @change="reset"><option value="">全部</option><option value="chat">对话</option><option value="streaming">流式输出</option><option value="tool_calling">工具调用</option><option value="reasoning">推理</option></select></label>
        <div class="filter-actions"><button type="button" @click="applySearch"><Search :size="15" />应用</button><button class="secondary-button" type="button" @click="clearFilters"><X :size="15" />清除</button></div>
      </div>
    </section>

    <section class="data-panel">
      <PanelHeader title="模型目录" :description="`第 ${page} 页 · 所有变更写入审计`"><StatePill :value="models.isFetching.value ? 'running' : 'active'" /></PanelHeader>
      <PaginatedTable aria-label="模型目录" :loading="models.isPending.value" :error="models.isError.value ? `模型目录读取失败：${models.error.value}` : ''" :empty="!(models.data.value?.models.length)" empty-title="没有匹配的模型" empty-description="调整筛选条件，或刷新模型目录。" :stale="models.isStale.value" :page="page" :total="models.data.value?.total" :can-previous="canPrevious.length > 0" :can-next="Boolean(models.data.value?.next_cursor)" @retry="models.refetch()" @previous="previous" @next="next(models.data.value?.next_cursor)">
        <template #header><tr><th>模型</th><th>提供方路线</th><th>能力</th><th>来源</th><th>状态</th><th>操作</th></tr></template>
        <tr v-for="model in models.data.value?.models ?? []" :key="model.model_id" :class="{ selected: selected?.model_id === model.model_id }"><td><button class="table-link" type="button" @click="selected = model"><strong>{{ model.display_name || model.model_id }}</strong><small class="mono">{{ model.model_id }}</small></button></td><td><div class="tag-list"><span v-for="route in model.routes" :key="route.provider"><span class="provider-mark" :class="`provider-mark--${route.provider}`">{{ route.provider }}</span>{{ route.enabled ? "启用" : "停用" }}</span></div></td><td><div class="tag-list"><span v-for="item in model.capabilities" :key="item">{{ item }}</span><span v-if="!model.capabilities.length">未声明</span></div></td><td>{{ model.source }}</td><td><StatePill :value="model.enabled" /></td><td><div class="row-actions"><button class="secondary-button compact-button" type="button" :disabled="probe.isPending.value" @click="probe.mutate(model)"><Activity :size="14" />探测</button><button class="icon-button" type="button" :aria-label="model.enabled ? `停用 ${model.model_id}` : `启用 ${model.model_id}`" :title="model.enabled ? `停用 ${model.model_id}` : `启用 ${model.model_id}`" :disabled="toggle.isPending.value" @click="requestToggle(model)"><Check :size="16" /></button></div></td></tr>
      </PaginatedTable>
    </section>

    <OperationStatus :operation="lastOperation" />

    <AccessibleDrawer :open="Boolean(selected)" :title="selected?.display_name || selected?.model_id || '模型详情'" :subtitle="selected ? `${selected.model_id} · ${enabledRouteCount(selected)}/${selected.routes.length} 路线启用` : ''" close-label="关闭模型详情" @close="selected = null">
      <template v-if="selected">
        <dl class="detail-list"><div><dt>状态</dt><dd><StatePill :value="selected.enabled" /></dd></div><div><dt>来源</dt><dd>{{ selected.source }}</dd></div><div><dt>能力</dt><dd>{{ selected.capabilities.join("、") || "未声明" }}</dd></div></dl>
        <h3>提供方路线</h3>
        <div class="tag-list"><span v-for="route in selected.routes" :key="route.provider"><span class="provider-mark" :class="`provider-mark--${route.provider}`">{{ route.provider }}</span><code class="mono">{{ route.upstream_id }}</code>{{ route.enabled ? "启用" : "停用" }}</span></div>
        <h3>当前筛选用量摘要</h3>
        <div v-if="modelUsage.isPending.value" class="loading-row">正在读取用量摘要…</div><div v-else-if="modelUsage.isError.value" class="data-state data-state--warning">用量摘要暂不可用，模型管理操作仍可继续。</div>
        <div v-else class="detail-metrics"><div><span>请求数</span><strong>{{ modelUsage.data.value?.summary.request_count ?? 0 }}</strong></div><div><span>错误率</span><strong>{{ errorRate(modelUsage.data.value?.summary) }}</strong></div><div><span>P95 延迟</span><strong>{{ modelUsage.data.value?.summary.latency_p95_ms != null ? `${modelUsage.data.value.summary.latency_p95_ms} ms` : "--" }}</strong></div></div>
        <button type="button" :disabled="probe.isPending.value" @click="probe.mutate(selected)"><Activity :size="16" />执行安全探测</button>
      </template>
    </AccessibleDrawer>

    <ConfirmDialog :open="Boolean(pendingDisable)" title="停用这个模型？" :description="`停用 ${pendingDisable?.model_id ?? ''} 后，该模型的所有提供方路线都将停用，新请求将无法再选择该模型；历史用量不受影响。`" confirm-label="确认停用" tone="danger" :busy="toggle.isPending.value" @cancel="pendingDisable = null" @confirm="pendingDisable && toggle.mutate(pendingDisable); pendingDisable = null" />
    <NotificationRegion :notifications="notifications" @dismiss="dismiss" />
  </section>
</template>
