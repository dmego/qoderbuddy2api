<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { Activity, ChevronRight, Filter, Plus, RefreshCcw, Search, ShieldCheck, Trash2, X } from "@lucide/vue";
import { computed, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { apiRequest } from "@/api/client";
import AccountImportPanel from "@/components/AccountImportPanel.vue";
import ConfirmDialog from "@/components/ConfirmDialog.vue";
import NotificationRegion from "@/components/NotificationRegion.vue";
import OperationStatus from "@/components/OperationStatus.vue";
import PaginatedTable from "@/components/PaginatedTable.vue";
import PanelHeader from "@/components/PanelHeader.vue";
import StatePill from "@/components/StatePill.vue";
import { appendQuery, useCursorPager } from "@/composables/useCursorPager";
import { useNotifications } from "@/composables/useNotifications";
import { statusLabel } from "@/utils/presentation";

type Purpose = { enabled: boolean; status: string; verification_status: string; expires_at?: string; last_error?: string };
type Account = {
  provider: string; account_id: string; label: string; source: string; enabled: boolean;
  summary_status: string; masked_identity?: string; shadowed?: boolean; metrics_status?: string;
  purposes: Record<string, Purpose>;
};
type AccountPage = { accounts: Account[]; next_cursor?: string | null; total?: number };
type Metric = { metric_kind: string; status: string; observed_at?: string; value: Record<string, unknown> | null; last_error?: string };

const queryClient = useQueryClient();
const router = useRouter();
const draftSearch = ref("");
const search = ref("");
const provider = ref("");
const source = ref("");
const statusFilter = ref("");
const purpose = ref("");
const showImport = ref(false);
const selection = ref<string[]>([]);
const pendingConfirm = ref<{ kind: "delete" | "disable" | "refresh" | "probe"; account?: Account } | null>(null);
const lastOperation = ref<Record<string, unknown> | null>(null);
const { cursor, page, canPrevious, next, previous, reset } = useCursorPager();
const { notifications, notify, dismiss } = useNotifications();

const accounts = useQuery({
  queryKey: ["accounts", cursor, search, provider, source, statusFilter, purpose],
  queryFn: () => apiRequest<AccountPage>(appendQuery("/accounts", { limit: 20, cursor: cursor.value, query: search.value, provider: provider.value, source: source.value, status: statusFilter.value, purpose: purpose.value })),
  staleTime: 15_000,
});
const metrics = useQuery({ queryKey: ["account-metrics", provider], queryFn: () => apiRequest<{ snapshots: (Metric & { provider: string; account_id: string })[] }>(appendQuery("/metrics/accounts", { provider: provider.value })), staleTime: 30_000 });

const action = useMutation({
  mutationFn: ({ account, kind }: { account: Account; kind: "toggle" | "promote" | "delete" | "refresh" | "probe" }) => mutateAccount(account, kind),
  onSuccess: async (result, input) => {
    lastOperation.value = { action: accountActionLabel(input.kind), account_id: input.account.account_id, status: "succeeded", ...asRecord(result) };
    notify(`${accountActionLabel(input.kind)}已完成`, { message: input.account.label, tone: "success" });
    await Promise.all([queryClient.invalidateQueries({ queryKey: ["accounts"] }), queryClient.invalidateQueries({ queryKey: ["account-metrics"] })]);
  },
  onError: (error) => notify("账号操作失败", { message: String(error), tone: "error", timeout: 0 }),
});
const batchAction = useMutation({
  mutationFn: async (kind: "refresh" | "probe" | "disable") => {
    const targets = currentAccounts().filter((item) => selection.value.includes(accountKey(item)));
    const items = await Promise.all(targets.map(async (account) => runBatchItem(account, kind)));
    const missing = selection.value.filter((key) => !targets.some((account) => accountKey(account) === key));
    items.push(...missing.map((key) => ({ key, label: key, status: "skipped", error: "account_not_on_current_page" })));
    const succeeded = items.filter((item) => item.status === "succeeded").length;
    const failed = items.filter((item) => item.status === "failed").length;
    const skipped = items.filter((item) => item.status === "skipped").length;
    const status = failed || skipped ? succeeded ? "partial" : "failed" : "succeeded";
    return { action: `批量${accountActionLabel(kind)}`, status, total: items.length, succeeded, failed, skipped, items };
  },
  onSuccess: async (result) => { lastOperation.value = result; notify("批量操作已结束", { message: `${result.succeeded} 成功 · ${result.failed} 失败 · ${result.skipped} 跳过`, tone: result.status === "succeeded" ? "success" : "warning" }); selection.value = []; await queryClient.invalidateQueries({ queryKey: ["accounts"] }); },
  onError: (error) => notify("批量操作失败", { message: String(error), tone: "error" }),
});

const mutating = computed(() => action.isPending.value || batchAction.isPending.value);
const allSelected = computed(() => selectableAccounts().length > 0 && selectableAccounts().every((item) => selection.value.includes(accountKey(item))));
watch([cursor, search, provider, source, statusFilter, purpose], () => { selection.value = []; });

function mutateAccount(account: Account, kind: "toggle" | "promote" | "delete" | "refresh" | "probe"): Promise<unknown> {
  const base = `/accounts/${encodeURIComponent(account.provider)}/${encodeURIComponent(account.account_id)}`;
  if (kind === "delete") return apiRequest(base, { method: "DELETE" });
  if (kind === "promote") return apiRequest(`${base}/promote`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ label: account.label }) });
  if (kind === "refresh" || kind === "probe") return apiRequest(`${base}/${kind}`, { method: "POST" });
  return apiRequest(base, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: !account.enabled, label: account.label }) });
}
function disableAccount(account: Account): Promise<unknown> { const base = `/accounts/${encodeURIComponent(account.provider)}/${encodeURIComponent(account.account_id)}`; return apiRequest(base, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: false, label: account.label }) }); }
async function runBatchItem(account: Account, kind: "refresh" | "probe" | "disable"): Promise<Record<string, unknown>> {
  if (kind === "disable" && isEnvAccount(account)) return batchItem(account, "skipped", "cannot_patch_env_account");
  try { await (kind === "disable" ? disableAccount(account) : mutateAccount(account, kind)); return batchItem(account, "succeeded"); }
  catch (error) { return batchItem(account, "failed", errorMessage(error)); }
}
function batchItem(account: Account, status: string, error = ""): Record<string, unknown> { return { key: accountKey(account), label: account.label, provider: account.provider, account_id: account.account_id, status, ...(error ? { error } : {}) }; }
function requestAction(account: Account, kind: "toggle" | "promote" | "delete" | "refresh" | "probe"): void { if (mutating.value) return; if (kind === "delete" || (kind === "toggle" && account.enabled)) pendingConfirm.value = { kind: kind === "toggle" ? "disable" : "delete", account }; else action.mutate({ account, kind }); }
function confirmPending(): void { if (mutating.value) return; const pending = pendingConfirm.value; pendingConfirm.value = null; if (!pending) return; if (pending.account) action.mutate({ account: pending.account, kind: pending.kind === "disable" ? "toggle" : "delete" }); else if (pending.kind !== "delete") batchAction.mutate(pending.kind); }
function requestBatch(kind: "refresh" | "probe" | "disable"): void { if (!mutating.value && selection.value.length) pendingConfirm.value = { kind }; }
function applySearch(): void { search.value = draftSearch.value.trim(); reset(); }
function clearFilters(): void { draftSearch.value = ""; search.value = ""; provider.value = ""; source.value = ""; statusFilter.value = ""; purpose.value = ""; reset(); }
function currentAccounts(): Account[] { return accounts.data.value?.accounts ?? []; }
function selectableAccounts(): Account[] { return currentAccounts().filter((account) => !isEnvAccount(account)); }
function isEnvAccount(account: Account): boolean { return account.source === "env"; }
function accountKey(account: Account): string { return `${account.provider}:${account.account_id}`; }
function toggleSelection(key: string): void { selection.value = selection.value.includes(key) ? selection.value.filter((item) => item !== key) : [...selection.value, key]; }
function toggleAll(): void { selection.value = allSelected.value ? [] : selectableAccounts().map(accountKey); }
function accountActionLabel(kind: string): string { return ({ toggle: "更新状态", promote: "提升账号", delete: "删除账号", refresh: "刷新账号", probe: "探测账号", disable: "停用账号" } as Record<string, string>)[kind] ?? kind; }
function asRecord(value: unknown): Record<string, unknown> { return typeof value === "object" && value !== null ? value as Record<string, unknown> : {}; }
function errorMessage(error: unknown): string { return error instanceof Error ? error.message : String(error); }
function openFullDetail(account: Account): void { void router.push({ name: "account-detail", params: { provider: account.provider, accountId: account.account_id } }); }
function metricStatus(account: Account): string { const rows = metrics.data.value?.snapshots ?? []; const found = rows.filter((item) => item.provider === account.provider && item.account_id === account.account_id); return account.metrics_status ?? (found.some((item) => item.status === "stale") ? "stale" : found.length ? "fresh" : "unavailable"); }
</script>

<template>
  <section class="page-content">
    <header class="page-header"><div><h1>账号管理</h1><p>按服务提供方、来源、状态和用途管理账号池，并对刷新与探测操作提供可追踪反馈。</p></div><button type="button" @click="showImport = !showImport"><Plus :size="16" />{{ showImport ? "收起导入" : "添加账号" }}</button></header>
    <AccountImportPanel v-if="showImport" @saved="queryClient.invalidateQueries({ queryKey: ['accounts'] })" />

    <section class="data-panel filter-panel">
      <PanelHeader title="账号筛选" description="搜索与筛选由管理 API 执行；选中项仅作用于当前页。"><Filter :size="17" /></PanelHeader>
      <div class="filter-grid filter-grid--six"><label class="filter-search">名称或账号 ID<div class="input-with-icon"><Search :size="15" /><input v-model="draftSearch" placeholder="回车搜索" @keyup.enter="applySearch" /></div></label><label>服务提供方<select v-model="provider" @change="reset"><option value="">全部</option><option value="codebuddy">CodeBuddy</option><option value="qoder">Qoder</option></select></label><label>来源<select v-model="source" @change="reset"><option value="">全部</option><option value="env">环境变量</option><option value="manual">手动导入</option><option value="oauth">OAuth 登录</option><option value="import">导入</option></select></label><label>状态<select v-model="statusFilter" @change="reset"><option value="">全部</option><option value="active">正常</option><option value="action_required">需处理</option><option value="pending">待验证</option><option value="disabled">停用</option></select></label><label>用途<select v-model="purpose" @change="reset"><option value="">全部</option><option value="chat">代理请求</option><option value="checkin">每日签到</option></select></label><div class="filter-actions"><button type="button" @click="applySearch"><Search :size="15" />应用</button><button class="secondary-button" type="button" @click="clearFilters"><X :size="15" />清除</button></div></div>
    </section>

    <section class="data-panel">
      <PanelHeader title="账号池" :description="`第 ${page} 页 · 环境变量账号只读`"><div class="toolbar"><span v-if="selection.length" class="selection-count">已选 {{ selection.length }}</span><button class="secondary-button compact-button" type="button" :disabled="!selection.length || mutating" @click="requestBatch('refresh')"><RefreshCcw :size="14" />批量刷新</button><button class="secondary-button compact-button" type="button" :disabled="!selection.length || mutating" @click="requestBatch('probe')"><Activity :size="14" />批量探测</button><button class="danger-button compact-button" type="button" :disabled="!selection.length || mutating" @click="requestBatch('disable')">批量停用</button></div></PanelHeader>
      <PaginatedTable aria-label="账号池" :loading="accounts.isPending.value" :error="accounts.isError.value ? `账号读取失败：${accounts.error.value}` : ''" :empty="!currentAccounts().length" empty-title="没有匹配的账号" empty-description="调整筛选条件，或通过上方导入账号。" :stale="accounts.isStale.value" :unavailable="metrics.isError.value" :page="page" :total="accounts.data.value?.total" :can-previous="canPrevious.length > 0" :can-next="Boolean(accounts.data.value?.next_cursor)" @retry="accounts.refetch()" @previous="previous" @next="next(accounts.data.value?.next_cursor)">
        <template #header><tr><th><input type="checkbox" aria-label="选择当前页全部可写账号" :checked="allSelected" :disabled="!selectableAccounts().length" @change="toggleAll" /></th><th>账号</th><th>服务提供方</th><th>用途</th><th>来源</th><th>指标</th><th>状态</th><th>操作</th></tr></template>
        <tr v-for="account in currentAccounts()" :key="accountKey(account)"><td><input type="checkbox" :aria-label="isEnvAccount(account) ? `环境变量账号不可选择 ${account.label}` : `选择 ${account.label}`" :checked="selection.includes(accountKey(account))" :disabled="isEnvAccount(account)" @change="toggleSelection(accountKey(account))" /></td><td><button class="table-link" type="button" :aria-label="`查看 ${account.label} 详情`" @click="openFullDetail(account)"><strong>{{ account.label }}</strong><small>{{ account.account_id }} · {{ account.masked_identity ?? "无身份掩码" }}</small></button></td><td><span class="provider-mark" :class="`provider-mark--${account.provider}`">{{ account.provider }}</span></td><td><div class="purpose-list"><span v-for="(item, name) in account.purposes" :key="name" class="purpose-chip"><b>{{ statusLabel(String(name)) }}</b><StatePill :value="item.status" /></span><span v-if="!Object.keys(account.purposes).length">未配置</span></div></td><td>{{ statusLabel(account.source) }}<span v-if="account.shadowed" class="text-warning"> · 被覆盖</span></td><td><StatePill :value="metricStatus(account)" /></td><td><StatePill :value="account.enabled ? account.summary_status : 'disabled'" /></td><td><div class="row-actions"><button class="icon-button" type="button" :aria-label="`刷新 ${account.label}`" :title="`刷新 ${account.label}`" :disabled="mutating" @click="requestAction(account, 'refresh')"><RefreshCcw :size="15" /></button><button class="icon-button" type="button" :aria-label="`探测 ${account.label}`" :title="`探测 ${account.label}`" :disabled="mutating" @click="requestAction(account, 'probe')"><Activity :size="15" /></button><button class="icon-button" type="button" :aria-label="isEnvAccount(account) ? `环境变量账号不可停用 ${account.label}` : account.enabled ? `停用 ${account.label}` : `启用 ${account.label}`" :title="account.enabled ? `停用 ${account.label}` : `启用 ${account.label}`" :disabled="isEnvAccount(account) || mutating" @click="requestAction(account, 'toggle')"><ShieldCheck :size="15" /></button><button v-if="account.source === 'env'" class="icon-button" type="button" :aria-label="`提升 ${account.label}`" :title="`提升 ${account.label}`" :disabled="mutating" @click="requestAction(account, 'promote')"><ChevronRight :size="16" /></button><button v-else class="icon-button danger-icon" type="button" :aria-label="`删除 ${account.label}`" :title="`删除 ${account.label}`" :disabled="mutating" @click="requestAction(account, 'delete')"><Trash2 :size="15" /></button></div></td></tr>
      </PaginatedTable>
    </section>

    <OperationStatus :operation="lastOperation" />
    <ConfirmDialog :open="Boolean(pendingConfirm)" :title="pendingConfirm?.account ? `${pendingConfirm.kind === 'delete' ? '删除' : '停用'} ${pendingConfirm.account.label}？` : `批量${accountActionLabel(pendingConfirm?.kind ?? '')}？`" :description="pendingConfirm?.kind === 'delete' ? '这会撤销持久化账号及其代理与签到用途，操作不可撤销。' : pendingConfirm?.account ? '停用后该账号不会再参与新的代理或签到调度。' : `将对当前选中的 ${selection.length} 个账号逐一执行，失败不会阻止其他账号。`" :confirm-label="pendingConfirm?.kind === 'delete' ? '确认删除' : '确认执行'" :tone="pendingConfirm?.kind === 'delete' || pendingConfirm?.kind === 'disable' ? 'danger' : 'default'" :busy="mutating" @cancel="pendingConfirm = null" @confirm="confirmPending" />

    <NotificationRegion :notifications="notifications" @dismiss="dismiss" />
  </section>
</template>
