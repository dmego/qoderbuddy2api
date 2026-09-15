<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import {
  Ban,
  ChevronDown,
  ChevronRight,
  CircleCheck,
  Info,
  RefreshCcw,
  RotateCcw,
  Save,
  Search,
  Sparkles,
  X,
} from "@lucide/vue";
import { computed, reactive, ref, watch } from "vue";

import { apiRequest } from "@/api/client";
import ConfirmDialog from "@/components/ConfirmDialog.vue";
import NotificationRegion from "@/components/NotificationRegion.vue";
import OperationStatus from "@/components/OperationStatus.vue";
import StatePill from "@/components/StatePill.vue";
import { useNotifications } from "@/composables/useNotifications";

type BlockInfo = { source: string; reason: string; blocked_until: string };
type RouteAccount = { account_id: string; label: string; source: string; blocked: boolean; block?: BlockInfo | null };
type RouteEntry = {
  provider: string; priority: number; weight: number; enabled: boolean;
  capabilities: string[]; accounts: RouteAccount[];
};
type RoutingModel = { model_id: string; name: string; configured: boolean; routes: RouteEntry[] };
type RouteDraft = { provider: string; priority: number; weight: number; enabled: boolean };
type RoutingResponse = { models: RoutingModel[] };

const INTL_PROVIDER = "workbuddy_intl";
const PROVIDER_LABELS: Record<string, string> = {
  codebuddy: "WorkBuddy",
  workbuddy_intl: "WorkBuddy 国际版",
};
const MAX_PRIORITY = 99;
const MAX_WEIGHT = 1000;

const queryClient = useQueryClient();
const { notifications, notify, dismiss } = useNotifications();
const draftSearch = ref("");
const search = ref("");
const onlyConfigured = ref(false);
const pendingReset = ref<RoutingModel | null>(null);
const lastOperation = ref<Record<string, unknown> | null>(null);
const drafts = reactive<Record<string, RouteDraft[]>>({});
// Collapsed by default: the page lists ~20 models and only a few matter at a time.
const expanded = reactive<Record<string, boolean>>({});
const showHelp = ref(false);

const routing = useQuery({
  queryKey: ["routing"],
  queryFn: () => apiRequest<RoutingResponse>("/routing"),
  staleTime: 15_000,
  refetchOnWindowFocus: false,
});
const models = computed(() => routing.data.value?.models ?? []);
const visibleModels = computed(() => {
  const needle = search.value.trim().toLowerCase();
  return models.value.filter((model) => {
    if (onlyConfigured.value && !model.configured) return false;
    if (!needle) return true;
    return model.model_id.toLowerCase().includes(needle) || model.name.toLowerCase().includes(needle);
  });
});
const configuredCount = computed(() => models.value.filter((model) => model.configured).length);
const blockedCount = computed(() => models.value.reduce(
  (total, model) => total + modelBlockedCount(model), 0,
));

watch(models, (list) => {
  for (const model of list) {
    drafts[model.model_id] = model.routes.map((route) => ({
      provider: route.provider, priority: route.priority, weight: route.weight, enabled: route.enabled,
    }));
  }
}, { immediate: true });

const save = useMutation({
  mutationFn: (model: RoutingModel) => apiRequest<{ status: string; model_id: string }>(`/routing/${encodeURIComponent(model.model_id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ routes: draftRoutes(model).map(payloadRoute) }),
  }),
  onSuccess: async (result, model) => {
    lastOperation.value = { action: "保存路由策略", status: "succeeded", model_id: result.model_id };
    notify("路由策略已保存", { message: `${model.model_id} 的优先级与权重已下发到代理。`, tone: "success" });
    await queryClient.invalidateQueries({ queryKey: ["routing"] });
  },
  onError: (error) => notify("路由策略保存失败", { message: String(error), tone: "error" }),
});
const reset = useMutation({
  mutationFn: (model: RoutingModel) => apiRequest<{ status: string; model_id: string }>(`/routing/${encodeURIComponent(model.model_id)}`, { method: "DELETE" }),
  onSuccess: async (result, model) => {
    lastOperation.value = { action: "恢复默认路由", status: "reset", model_id: result.model_id };
    notify("已恢复默认轮询", { message: `${model.model_id} 现在由全部提供方同一梯队等权轮询。`, tone: "success" });
    pendingReset.value = null;
    await queryClient.invalidateQueries({ queryKey: ["routing"] });
  },
  onError: (error) => notify("恢复默认失败", { message: String(error), tone: "error" }),
});
const accountBlock = useMutation({
  mutationFn: (payload: { model: RoutingModel; provider: string; account: RouteAccount; blocked: boolean }) =>
    apiRequest<{ status: string }>(
      `/routing/${encodeURIComponent(payload.model.model_id)}/accounts/${encodeURIComponent(payload.provider)}/${encodeURIComponent(payload.account.account_id)}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ blocked: payload.blocked, reason: payload.blocked ? "管理员手动停用" : "" }),
      },
    ),
  onSuccess: async (_result, payload) => {
    const who = `${providerLabel(payload.provider)} · ${payload.account.account_id}`;
    notify(payload.blocked ? "已停用该账号的此模型" : "已恢复该账号的此模型", {
      message: payload.blocked
        ? `${who} 不再承接 ${payload.model.model_id}，请求会走其他账号。`
        : `${who} 重新参与 ${payload.model.model_id} 的流量分配。`,
      tone: "success",
    });
    await queryClient.invalidateQueries({ queryKey: ["routing"] });
  },
  onError: (error) => notify("账号停用操作失败", { message: String(error), tone: "error" }),
});

function draftRoutes(model: RoutingModel): RouteDraft[] {
  return drafts[model.model_id] ?? model.routes.map((route) => ({
    provider: route.provider, priority: route.priority, weight: route.weight, enabled: route.enabled,
  }));
}

function payloadRoute(route: RouteDraft): RouteDraft {
  return {
    provider: route.provider,
    priority: clamp(route.priority, MAX_PRIORITY),
    weight: route.enabled ? clamp(route.weight, MAX_WEIGHT) : 0,
    enabled: route.enabled,
  };
}

function clamp(value: number, maximum: number): number {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 0;
  return Math.min(Math.max(Math.trunc(numeric), 0), maximum);
}

function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider] ?? provider;
}

function accountsFor(model: RoutingModel, provider: string): RouteAccount[] {
  return routeAccounts(model.routes.find((route) => route.provider === provider));
}

function routeAccounts(route?: RouteEntry): RouteAccount[] {
  return Array.isArray(route?.accounts) ? route.accounts : [];
}

function hasIntlRoute(model: RoutingModel): boolean {
  return model.routes.some((route) => route.provider === INTL_PROVIDER);
}

function isDirty(model: RoutingModel): boolean {
  return JSON.stringify(draftRoutes(model)) !== JSON.stringify(model.routes.map((route) => ({
    provider: route.provider, priority: route.priority, weight: route.weight, enabled: route.enabled,
  })));
}

function validationHint(model: RoutingModel): string {
  const routes = draftRoutes(model);
  if (!routes.length) return "该模型当前没有可用路线，无法保存策略。";
  if (!routes.some((route) => route.enabled)) return "至少要保留一条启用的路线，否则该模型无法再被请求。";
  if (!routes.some((route) => route.enabled && route.weight > 0)) return "至少一条启用路线的权重需要大于 0，否则同优先级内无法分配流量。";
  return "";
}

function tierIndex(model: RoutingModel, priority: number): number {
  const tiers = [...new Set(draftRoutes(model).map((route) => clamp(route.priority, MAX_PRIORITY)))].sort((left, right) => left - right);
  return tiers.indexOf(clamp(priority, MAX_PRIORITY)) + 1;
}

function blockedOnRoute(model: RoutingModel, provider: string): number {
  return accountsFor(model, provider).filter((account) => account.blocked).length;
}

function modelBlockedCount(model: RoutingModel): number {
  return model.routes.reduce(
    (sum, route) => sum + routeAccounts(route).filter((account) => account.blocked).length, 0,
  );
}

function modelAccountCount(model: RoutingModel): number {
  return model.routes.reduce((sum, route) => sum + routeAccounts(route).length, 0);
}

function toggle(model: RoutingModel): void {
  expanded[model.model_id] = !expanded[model.model_id];
}

function setAllExpanded(value: boolean): void {
  for (const model of visibleModels.value) expanded[model.model_id] = value;
}

function applyIntlPreset(model: RoutingModel): void {
  drafts[model.model_id] = model.routes.map((route) => ({
    provider: route.provider,
    priority: route.provider === INTL_PROVIDER ? 0 : 1,
    weight: 1,
    enabled: true,
  }));
  notify("已套用「国际版优先」", { message: `${model.model_id}：WorkBuddy 国际版优先，其余提供方作为回退，正在保存…`, tone: "info" });
  save.mutate(model);
}

function formatReset(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  return at.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function blockLabel(block?: BlockInfo | null): string {
  if (!block) return "已停用";
  const why = block.reason || (block.source === "auto" ? "上游配额受限" : "管理员手动停用");
  return block.blocked_until ? `${why} · 预计 ${formatReset(block.blocked_until)} 恢复` : why;
}

function onAccountToggle(model: RoutingModel, provider: string, account: RouteAccount, event: Event): void {
  const checked = (event.target as HTMLInputElement).checked;
  accountBlock.mutate({ model, provider, account, blocked: !checked });
}

function applySearch(): void { search.value = draftSearch.value; }
function clearFilters(): void { draftSearch.value = ""; search.value = ""; onlyConfigured.value = false; }
</script>

<template>
  <section class="page-content">
    <header class="page-header">
      <div><h1>路由策略</h1><p>为每个模型指定提供方的尝试顺序、流量分配，以及账号级的模型停用。</p></div>
      <div class="header-actions">
        <button class="secondary-button compact-button" type="button" @click="setAllExpanded(true)"><ChevronDown :size="14" />全部展开</button>
        <button class="secondary-button compact-button" type="button" @click="setAllExpanded(false)"><ChevronRight :size="14" />全部收起</button>
        <button class="secondary-button compact-button" type="button" :disabled="routing.isFetching.value" @click="routing.refetch()"><RefreshCcw :class="{ spin: routing.isFetching.value }" :size="14" />读取</button>
      </div>
    </header>

    <section class="data-panel filter-panel">
      <div class="routing-toolbar">
        <label class="routing-search"><Search :size="14" /><input v-model="draftSearch" aria-label="模型名称或 ID" placeholder="模型名称或 ID，回车搜索" @keyup.enter="applySearch" /></label>
        <label class="routing-inline-check"><input v-model="onlyConfigured" type="checkbox" aria-label="仅已自定义" />仅已自定义</label>
        <button class="compact-button" type="button" @click="applySearch">应用</button>
        <button class="secondary-button compact-button" type="button" @click="clearFilters"><X :size="13" />清除</button>
        <span class="routing-count">{{ visibleModels.length }}/{{ models.length }} 个模型 · {{ configuredCount }} 个已自定义<span v-if="blockedCount"> · {{ blockedCount }} 个账号停用</span></span>
        <button class="routing-help-toggle" type="button" :aria-expanded="showHelp" @click="showHelp = !showHelp"><Info :size="13" />字段说明</button>
      </div>
      <div v-if="showHelp" class="routing-legend">
        <div><strong>优先级</strong><span>数字越小越先尝试；相同值构成一个梯队，上一梯队整体不可用时才用下一梯队。</span></div>
        <div><strong>权重</strong><span>同一梯队内的相对流量份额（平滑加权轮询）。3 与 1 表示 3:1，0 表示该梯队内不接流量。</span></div>
        <div><strong>路线启用</strong><span>关闭后该提供方只在其他路线都无法服务该模型时才兜底；关闭的路线权重按 0 处理。</span></div>
        <div><strong>账号停用</strong><span>按「账号 × 模型」停用，只影响这个模型。上游配额受限的账号会自动停用并注明原因与恢复时间。</span></div>
      </div>
    </section>

    <section v-if="routing.isPending.value" class="data-panel"><div class="loading-row">正在读取模型路由策略…</div></section>
    <section v-else-if="routing.isError.value" class="data-panel"><div class="empty-state">路由策略读取失败：{{ routing.error.value }}<button class="secondary-button compact-button" type="button" @click="routing.refetch()">重试</button></div></section>
    <div v-else-if="!visibleModels.length" class="data-panel"><div class="compact-empty">没有匹配的模型，调整筛选条件后重试。</div></div>

    <template v-else>
      <section v-for="model in visibleModels" :key="model.model_id" class="data-panel routing-model">
        <button class="routing-model__head" type="button" :aria-expanded="Boolean(expanded[model.model_id])" @click="toggle(model)">
          <component :is="expanded[model.model_id] ? ChevronDown : ChevronRight" :size="15" class="routing-model__caret" />
          <span class="routing-model__title">
            <span class="routing-model__name">{{ model.name || model.model_id }}</span>
            <code class="routing-model__id">{{ model.model_id }}</code>
          </span>
          <span class="routing-model__meta">
            <span v-for="route in model.routes" :key="route.provider" class="routing-chip" :class="{ 'routing-chip--off': !route.enabled }">{{ providerLabel(route.provider) }} P{{ route.priority }}/W{{ route.weight }}</span>
          </span>
          <span class="routing-flag">{{ modelBlockedCount(model) ? `${modelBlockedCount(model)} 账号停用` : "" }}</span>
          <StatePill :value="model.configured ? 'effective' : 'static'" />
        </button>

        <div v-if="expanded[model.model_id]" class="routing-model__body">
          <div class="routing-actions">
            <button v-if="hasIntlRoute(model)" class="secondary-button compact-button" type="button" :disabled="save.isPending.value" title="把 WorkBuddy 国际版设为最先尝试，其余提供方作为回退，并立即保存" @click="applyIntlPreset(model)"><Sparkles :size="13" />国际版优先</button>
            <button class="compact-button" type="button" :disabled="save.isPending.value || !isDirty(model) || Boolean(validationHint(model))" @click="save.mutate(model)"><Save :size="13" />保存</button>
            <button class="secondary-button compact-button" type="button" :disabled="reset.isPending.value || !model.configured" @click="pendingReset = model"><RotateCcw :size="13" />恢复默认</button>
            <span class="routing-actions__note">{{ model.configured ? "自定义策略" : "默认等权轮询" }} · {{ modelAccountCount(model) }} 个可用账号</span>
          </div>

          <div v-for="route in draftRoutes(model)" :key="route.provider" class="routing-route">
            <div class="routing-route__grid">
              <span class="provider-mark" :class="`provider-mark--${route.provider}`">{{ providerLabel(route.provider) }}</span>
              <span v-if="!accountsFor(model, route.provider).length" class="routing-note">暂无可用账号</span>
              <span v-else-if="blockedOnRoute(model, route.provider)" class="routing-note routing-note--warn">{{ blockedOnRoute(model, route.provider) }} 个账号停用</span>
              <label class="routing-field">优先级<input v-model.number="route.priority" type="number" min="0" :max="MAX_PRIORITY" :aria-label="`${model.model_id} ${route.provider} 优先级`" /></label>
              <label class="routing-field">权重<input v-model.number="route.weight" type="number" min="0" :max="MAX_WEIGHT" :disabled="!route.enabled" :aria-label="`${model.model_id} ${route.provider} 权重`" /></label>
              <label class="routing-inline-check"><input v-model="route.enabled" type="checkbox" :aria-label="`${model.model_id} ${route.provider} 启用`" />启用</label>
              <span class="routing-tier">第 {{ tierIndex(model, route.priority) }} 梯队</span>
            </div>

            <div v-if="accountsFor(model, route.provider).length" class="routing-accounts">
              <label v-for="account in accountsFor(model, route.provider)" :key="account.account_id" class="routing-account" :class="{ 'routing-account--blocked': account.blocked }">
                <input type="checkbox" :checked="!account.blocked" :disabled="accountBlock.isPending.value" :aria-label="`${model.model_id} ${account.account_id} 参与路由`" @change="onAccountToggle(model, route.provider, account, $event)" />
                <span class="routing-account__label">{{ account.label || account.account_id }}</span>
                <code class="routing-account__id">{{ account.account_id }}</code>
                <span v-if="account.blocked" class="routing-account__why"><Ban :size="12" />{{ blockLabel(account.block) }}</span>
                <span v-else class="routing-account__ok"><CircleCheck :size="12" />参与</span>
              </label>
            </div>
          </div>

          <p v-if="validationHint(model)" class="helper-text text-danger">{{ validationHint(model) }}</p>
          <p v-else-if="isDirty(model)" class="helper-text">有未保存的修改：保存后策略会立即下发给代理服务。</p>
        </div>
      </section>
    </template>

    <OperationStatus :operation="lastOperation" />

    <ConfirmDialog
      :open="Boolean(pendingReset)"
      title="恢复默认轮询？"
      :description="`将删除 ${pendingReset?.model_id ?? ''} 的自定义路由策略，该模型会回到全部提供方同一梯队、等权轮询。`"
      confirm-label="确认恢复"
      tone="danger"
      :busy="reset.isPending.value"
      @cancel="pendingReset = null"
      @confirm="pendingReset && reset.mutate(pendingReset)"
    />
    <NotificationRegion :notifications="notifications" @dismiss="dismiss" />
  </section>
</template>

<style scoped>
/* Compact console typography: design tokens only, one size throughout. */
.routing-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 10px 16px; font-size: var(--text-xs); }
.routing-search { display: flex; align-items: center; gap: 6px; min-width: 220px; padding: 0 8px; border: 1px solid var(--line-strong); border-radius: var(--radius); background: var(--canvas); }
.routing-search input { min-height: var(--control-h-sm); border: 0; background: transparent; font-size: var(--text-xs); }
.routing-inline-check { display: inline-flex; align-items: center; gap: 5px; color: var(--muted); font-size: var(--text-xs); white-space: nowrap; }
.routing-count { margin-left: auto; color: var(--muted); font-size: var(--text-xs); }
.routing-help-toggle { display: inline-flex; align-items: center; gap: 4px; padding: 0; border: 0; background: none; color: var(--muted); font-size: var(--text-xs); cursor: pointer; }
.routing-help-toggle:hover { color: var(--text); }
.routing-legend { display: grid; gap: 4px; padding: 0 16px 12px; }
.routing-legend > div { display: flex; gap: 8px; font-size: var(--text-xs); line-height: 1.6; }
.routing-legend strong { flex: 0 0 72px; color: var(--text); font-weight: 500; }
.routing-legend span { color: var(--muted); }

.routing-model { padding: 0; }
/* Fixed tracks keep every row scannable down the page instead of ragged. */
.routing-model__head {
  display: grid; grid-template-columns: 16px minmax(150px, 200px) minmax(220px, 1fr) 96px 88px;
  align-items: center; gap: 10px; width: 100%; min-height: 0; padding: 8px 16px;
  border: 0; border-radius: 0; background: none; color: inherit;
  font-size: var(--text-xs); font-weight: 400; text-align: left; cursor: pointer;
}
.routing-model__head:hover { background: var(--surface-raised); }
.routing-model__caret { color: var(--muted); }
.routing-model__title { display: flex; align-items: baseline; gap: 6px; min-width: 0; }
.routing-model__name { overflow: hidden; color: var(--text); font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.routing-model__id { overflow: hidden; color: var(--muted); font-family: var(--mono); font-size: var(--text-xs); text-overflow: ellipsis; white-space: nowrap; }
.routing-model__meta { display: flex; flex-wrap: wrap; gap: 4px; min-width: 0; }
.routing-chip { padding: 1px 7px; border: 1px solid var(--line); border-radius: 999px; color: var(--muted); font-size: var(--text-xs); }
.routing-chip--off { opacity: .55; text-decoration: line-through; }
.routing-flag { color: var(--warn); font-size: var(--text-xs); text-align: right; white-space: nowrap; }

.routing-model__body { display: grid; gap: 10px; padding: 10px 14px 12px; border-top: 1px solid var(--line); }
.routing-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.routing-actions__note { margin-left: auto; color: var(--muted); font-size: var(--text-xs); }
.routing-route { display: grid; gap: 6px; padding: 8px 0; border-top: 1px dashed var(--line); }
.routing-route:first-of-type { border-top: 0; }
.routing-route__grid { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; font-size: var(--text-xs); }
.routing-field { display: inline-flex; align-items: center; gap: 5px; color: var(--muted); font-size: var(--text-xs); }
.routing-field input { width: 68px; min-height: var(--control-h-sm); padding: 0 6px; font-size: var(--text-xs); }
.routing-tier { color: var(--muted); font-size: var(--text-xs); }
.routing-note { color: var(--muted); font-size: var(--text-xs); white-space: nowrap; }
.routing-note--warn { color: var(--warn); }

.routing-accounts { display: flex; flex-wrap: wrap; gap: 6px; }
.routing-account { display: inline-flex; align-items: center; gap: 6px; padding: 3px 9px; border: 1px solid var(--line); border-radius: 999px; font-size: var(--text-xs); }
.routing-account--blocked { border-color: var(--warn-line); background: var(--warn-soft); }
.routing-account__label { color: var(--text); }
.routing-account__id { color: var(--muted); font-family: var(--mono); font-size: var(--text-xs); }
.routing-account__ok { display: inline-flex; align-items: center; gap: 3px; color: var(--muted); }
.routing-account__why { display: inline-flex; align-items: center; gap: 3px; color: var(--warn); }
</style>
