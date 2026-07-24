<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { ClipboardCheck, DatabaseBackup, Eye, Filter, History, RefreshCcw, Search, ShieldCheck, X } from "@lucide/vue";
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

type AuditEvent = { event_id: string; actor_type?: string; action: string; resource_type: string; resource_id?: string; result: string; created_at: string; error_code?: string };
type AuditPage = { events: AuditEvent[]; next_cursor?: string | null; total?: number };
type Backup = { backup_id: string; path?: string; schema_version: string; started_at: string; finished_at?: string; status: string; size_bytes?: number; sha256?: string; error_message?: string };

const queryClient = useQueryClient();
const draftSearch = ref("");
const draftActionPrefix = ref("");
const search = ref("");
const actionPrefix = ref("");
const category = ref("");
const resourceType = ref("");
const resultFilter = ref("");
const pendingAction = ref<{ kind: "create" | "validate"; backup?: Backup } | null>(null);
const selectedBackup = ref<Backup | null>(null);
const backupPage = ref(1);
const backupPageSize = 10;
const lastOperation = ref<Record<string, unknown> | null>(null);
const { cursor, page, canPrevious, next, previous, reset } = useCursorPager();
const { notifications, notify, dismiss } = useNotifications();

const audit = useQuery({ queryKey: ["audit", cursor, search, actionPrefix, category, resourceType, resultFilter], queryFn: () => apiRequest<AuditPage>(appendQuery("/audit", { limit: 25, cursor: cursor.value, search: search.value, action_prefix: actionPrefix.value, category: category.value, resource_type: resourceType.value, result: resultFilter.value })), staleTime: 30_000 });
const backups = useQuery({ queryKey: ["backups"], queryFn: () => apiRequest<{ backups: Backup[] }>("/backup"), staleTime: 30_000 });
const visibleBackups = computed(() => (backups.data.value?.backups ?? []).slice((backupPage.value - 1) * backupPageSize, backupPage.value * backupPageSize));
const backupCount = computed(() => backups.data.value?.backups.length ?? 0);
const operation = useMutation({
  mutationFn: async (input: { kind: "create" | "validate"; backup?: Backup }) => input.kind === "create" ? apiRequest<Record<string, unknown>>("/backup", { method: "POST" }) : apiRequest<Record<string, unknown>>(`/backup/${encodeURIComponent(input.backup?.backup_id ?? "")}/restore`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ dry_run: true }) }),
  onSuccess: async (result, input) => { lastOperation.value = { action: input.kind === "create" ? "创建 SQLite 备份" : "校验离线恢复", status: "succeeded", ...result }; notify(input.kind === "create" ? "备份已创建" : "恢复校验通过", { message: input.kind === "validate" ? "在线 Control Plane 未覆盖数据库。" : String(result.backup_id ?? "快照已写入"), tone: "success" }); await Promise.all([queryClient.invalidateQueries({ queryKey: ["backups"] }), queryClient.invalidateQueries({ queryKey: ["audit"] })]); },
  onError: (error, input) => notify(input.kind === "create" ? "备份创建失败" : "恢复校验失败", { message: String(error), tone: "error", timeout: 0 }),
});

function confirmOperation(): void { const pending = pendingAction.value; pendingAction.value = null; if (pending) operation.mutate(pending); }
function applyTextFilters(): void { reset(); search.value = draftSearch.value.trim(); actionPrefix.value = draftActionPrefix.value.trim(); }
function updateCategory(event: Event): void { reset(); category.value = selectValue(event); }
function updateResourceType(event: Event): void { reset(); resourceType.value = selectValue(event); }
function updateResult(event: Event): void { reset(); resultFilter.value = selectValue(event); }
function clearFilters(): void { reset(); draftSearch.value = ""; draftActionPrefix.value = ""; search.value = ""; actionPrefix.value = ""; category.value = ""; resourceType.value = ""; resultFilter.value = ""; }
function selectValue(event: Event): string { return (event.target as HTMLSelectElement).value; }
function size(value?: number): string { if (value === undefined) return "--"; if (value < 1024) return `${value} B`; if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`; return `${(value / 1024 / 1024).toFixed(1)} MB`; }
</script>

<template>
  <section class="page-content">
    <header class="page-header"><div><p class="eyebrow">Audit & recovery</p><h1>审计与备份</h1><p>追踪管理变更，创建 SQLite 快照，并在离线恢复前执行 checksum、schema 与 integrity dry-run。</p></div><button type="button" :disabled="operation.isPending.value" @click="pendingAction = { kind: 'create' }"><DatabaseBackup :size="16" />创建 SQLite 备份</button></header>
    <div class="security-banner"><ShieldCheck :size="18" /><div><strong>恢复保护</strong><span>管理台只调用 dry-run 校验，不会在线覆盖数据库；凭据主密钥和 .env 不包含在 SQLite 备份中。</span></div></div>

    <section class="data-panel"><PanelHeader title="备份快照" :description="`${backupCount} 个快照 · 第 ${backupPage} 页`"><button class="icon-button" type="button" aria-label="刷新备份列表" @click="backups.refetch()"><RefreshCcw :size="16" /></button></PanelHeader><PaginatedTable aria-label="备份快照" :loading="backups.isPending.value" :error="backups.isError.value ? `备份列表读取失败：${backups.error.value}` : ''" :empty="!visibleBackups.length" empty-title="还没有备份快照" empty-description="创建备份后，可在这里核对 checksum 和 schema version。" :stale="backups.isStale.value" :page="backupPage" :page-size="backupPageSize" :total="backupCount" :can-previous="backupPage > 1" :can-next="backupPage * backupPageSize < backupCount" @retry="backups.refetch()" @previous="backupPage -= 1" @next="backupPage += 1"><template #header><tr><th>Backup ID / 时间</th><th>状态</th><th>Schema</th><th>大小</th><th>SHA-256</th><th>操作</th></tr></template><tr v-for="item in visibleBackups" :key="item.backup_id" :class="{ selected: selectedBackup?.backup_id === item.backup_id }"><td><strong class="mono">{{ item.backup_id }}</strong><small>{{ item.started_at }}</small></td><td><StatePill :value="item.status" /><small v-if="item.error_message" class="text-danger">{{ item.error_message }}</small></td><td>v{{ item.schema_version }}</td><td>{{ size(item.size_bytes) }}</td><td class="mono hash-cell" :title="item.sha256">{{ item.sha256 ?? "--" }}</td><td><div class="row-actions"><button class="icon-button" type="button" :aria-label="`查看备份 ${item.backup_id}`" @click="selectedBackup = item"><Eye :size="15" /></button><button class="secondary-button compact-button" type="button" :disabled="operation.isPending.value || item.status !== 'succeeded'" @click="pendingAction = { kind: 'validate', backup: item }"><ClipboardCheck :size="15" />校验恢复</button></div></td></tr></PaginatedTable></section>

    <section class="data-panel filter-panel">
      <PanelHeader title="管理审计" :description="`第 ${page} 页 · 只展示操作元数据`"><Filter :size="17" /></PanelHeader><div class="filter-grid filter-grid--six"><label class="filter-search">搜索<input v-model="draftSearch" aria-label="审计搜索" placeholder="事件、动作或资源 ID" @keyup.enter="applyTextFilters" /></label><label>动作前缀<input v-model="draftActionPrefix" aria-label="动作前缀" placeholder="例如 account" @keyup.enter="applyTextFilters" /></label><label>类别<select :value="category" aria-label="审计类别" @change="updateCategory"><option value="">全部</option><option value="account">账号</option><option value="backup">备份</option><option value="checkin">签到</option><option value="credential">凭据</option><option value="metrics">指标</option><option value="model">模型</option><option value="proxy_key">代理密钥</option><option value="service">服务</option><option value="settings">设置</option><option value="usage">用量</option></select></label><label>资源<select :value="resourceType" aria-label="审计资源" @change="updateResourceType"><option value="">全部</option><option value="account">Account</option><option value="backup">Backup</option><option value="checkin">Check-in</option><option value="credential">Credential</option><option value="metrics">Metrics</option><option value="model">Model</option><option value="proxy_key">Proxy key</option><option value="service">Service</option><option value="setting">Setting</option><option value="usage">Usage</option></select></label><label>结果<select :value="resultFilter" aria-label="审计结果" @change="updateResult"><option value="">全部</option><option value="succeeded">成功</option><option value="failed">失败</option><option value="running">运行中</option><option value="cancelled">已取消</option></select></label><div class="filter-actions"><button type="button" @click="applyTextFilters"><Search :size="15" />应用</button><button class="secondary-button" type="button" @click="clearFilters"><X :size="15" />清除</button></div></div>
      <PaginatedTable aria-label="管理审计事件" :loading="audit.isPending.value" :error="audit.isError.value ? `审计读取失败：${audit.error.value}` : ''" :empty="!(audit.data.value?.events.length)" empty-title="暂无匹配的审计记录" empty-description="管理变更发生后，安全元数据会持久化到这里。" :stale="audit.isStale.value" :page="page" :page-size="25" :total="audit.data.value?.total" :can-previous="canPrevious.length > 0" :can-next="Boolean(audit.data.value?.next_cursor)" @retry="audit.refetch()" @previous="previous" @next="next(audit.data.value?.next_cursor)"><template #header><tr><th>时间</th><th>动作</th><th>资源</th><th>Actor</th><th>结果</th><th>事件 ID</th></tr></template><tr v-for="event in audit.data.value?.events ?? []" :key="event.event_id"><td>{{ event.created_at }}</td><td><History :size="14" /> {{ event.action }}</td><td>{{ event.resource_type }}<small>{{ event.resource_id ?? "--" }}</small></td><td>{{ event.actor_type ?? "admin" }}</td><td><StatePill :value="event.result" /><small v-if="event.error_code" class="text-danger">{{ event.error_code }}</small></td><td class="mono">{{ event.event_id }}</td></tr></PaginatedTable>
    </section>

    <OperationStatus :operation="lastOperation" />
    <AccessibleDrawer :open="Boolean(selectedBackup)" title="备份详情" eyebrow="Backup detail" :subtitle="selectedBackup?.backup_id ?? ''" close-label="关闭备份详情" @close="selectedBackup = null"><template v-if="selectedBackup"><dl class="detail-list"><div><dt>状态</dt><dd><StatePill :value="selectedBackup.status" /></dd></div><div><dt>Schema</dt><dd>v{{ selectedBackup.schema_version }}</dd></div><div><dt>大小</dt><dd>{{ size(selectedBackup.size_bytes) }}</dd></div><div><dt>SHA-256</dt><dd class="mono">{{ selectedBackup.sha256 ?? "--" }}</dd></div><div><dt>完成时间</dt><dd>{{ selectedBackup.finished_at ?? "--" }}</dd></div><div><dt>错误</dt><dd :class="{ 'text-danger': selectedBackup.error_message }">{{ selectedBackup.error_message ?? "无" }}</dd></div></dl></template></AccessibleDrawer>

    <ConfirmDialog :open="Boolean(pendingAction)" :title="pendingAction?.kind === 'create' ? '创建数据库备份？' : '校验离线恢复？'" :description="pendingAction?.kind === 'create' ? '将创建一致性 SQLite 快照并记录审计事件，不包含 .env 或凭据主密钥。' : '只执行 checksum、schema 与 integrity dry-run，不会覆盖当前数据库。'" :confirm-label="pendingAction?.kind === 'create' ? '创建备份' : '开始校验'" :busy="operation.isPending.value" @cancel="pendingAction = null" @confirm="confirmOperation" />
    <NotificationRegion :notifications="notifications" @dismiss="dismiss" />
  </section>
</template>
