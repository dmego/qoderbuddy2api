<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { ClipboardList, DatabaseBackup, Download, History, RefreshCcw, ShieldCheck } from "@lucide/vue";
import { ref } from "vue";

import PanelHeader from "@/components/PanelHeader.vue";
import StatePill from "@/components/StatePill.vue";
import { apiRequest } from "@/api/client";

type AuditEvent = { event_id: string; action: string; resource_type: string; resource_id?: string; result: string; created_at: string; metadata_json: string };
type Backup = { backup_id: string; path: string; schema_version: string; started_at: string; finished_at?: string; status: string; size_bytes?: number; sha256?: string; error_message?: string };
const queryClient = useQueryClient();
const audit = useQuery({ queryKey: ["audit"], queryFn: () => apiRequest<{ events: AuditEvent[] }>("/audit") });
const backups = useQuery({ queryKey: ["backups"], queryFn: () => apiRequest<{ backups: Backup[] }>("/backup") });
const backup = useMutation({ mutationFn: () => apiRequest<Backup>("/backup", { method: "POST" }), onSuccess: () => queryClient.invalidateQueries({ queryKey: ["backups"] }) });
const restore = useMutation({ mutationFn: (id: string) => apiRequest(`/backup/${id}/restore`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ dry_run: true }) }) });
const message = ref("");
function size(value?: number): string { if (!value) return "--"; return `${(value / 1024).toFixed(1)} KB`; }
async function validate(id: string): Promise<void> { const result = await restore.mutateAsync(id); message.value = `备份 ${id} 校验通过，恢复仍需离线操作。`; console.info(result); }
</script>

<template>
  <section class="page-content">
    <header class="page-header"><div><p class="eyebrow">Audit & recovery</p><h1>审计与备份</h1><p>追踪所有管理变更，并在覆盖数据库前执行只读完整性校验。</p></div><button type="button" :disabled="backup.isPending.value" @click="backup.mutate()"><DatabaseBackup :size="16" />创建 SQLite 备份</button></header>
    <div class="security-banner"><ShieldCheck :size="18" /><div><strong>恢复保护</strong><span>当前恢复接口只做 checksum/schema/integrity dry-run，不会在在线 Control Plane 中覆盖数据库。</span></div></div>
    <section class="data-panel"><PanelHeader title="备份快照" description="凭据主密钥和 .env 不会被打包进数据库备份"><template #default><button class="icon-button" type="button" title="刷新备份列表" @click="backups.refetch()"><RefreshCcw :size="16" /></button></template></PanelHeader><div v-if="!backups.data.value?.backups.length" class="compact-empty">还没有备份快照</div><div v-else class="table-wrap"><table><thead><tr><th>Backup ID</th><th>状态</th><th>Schema</th><th>大小</th><th>SHA-256</th><th>操作</th></tr></thead><tbody><tr v-for="item in backups.data.value.backups" :key="item.backup_id"><td class="mono">{{ item.backup_id }}</td><td><StatePill :value="item.status" /></td><td>v{{ item.schema_version }}</td><td>{{ size(item.size_bytes) }}</td><td class="mono hash-cell">{{ item.sha256 ?? "--" }}</td><td><button class="secondary-button compact-button" type="button" :disabled="restore.isPending.value" @click="validate(item.backup_id)"><Download :size="15" />校验恢复</button></td></tr></tbody></table></div></section>
    <section class="data-panel"><PanelHeader title="管理审计" description="只显示操作元数据，不记录凭据正文或请求内容"><template #default><History :size="17" /></template></PanelHeader><div v-if="!audit.data.value?.events.length" class="compact-empty">暂无审计记录</div><div v-else class="table-wrap"><table><thead><tr><th>时间</th><th>动作</th><th>资源</th><th>结果</th><th>事件 ID</th></tr></thead><tbody><tr v-for="event in audit.data.value.events" :key="event.event_id"><td>{{ event.created_at }}</td><td><ClipboardList :size="15" /> {{ event.action }}</td><td>{{ event.resource_type }} / {{ event.resource_id ?? "--" }}</td><td><StatePill :value="event.result" /></td><td class="mono">{{ event.event_id }}</td></tr></tbody></table></div></section>
    <p v-if="message" class="form-message">{{ message }}</p><p v-if="backup.isError.value || restore.isError.value" class="form-error">操作失败：{{ backup.error.value ?? restore.error.value }}</p>
  </section>
</template>
