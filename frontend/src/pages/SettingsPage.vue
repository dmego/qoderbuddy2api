<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { Check, CircleHelp, Save, Settings2 } from "@lucide/vue";
import { computed, reactive, watch } from "vue";

import PanelHeader from "@/components/PanelHeader.vue";
import StatePill from "@/components/StatePill.vue";
import { apiRequest } from "@/api/client";

type Setting = { key: string; value: unknown; value_version: number; apply_mode: string; apply_status: string; last_error?: string };
const queryClient = useQueryClient();
const settings = useQuery({ queryKey: ["settings"], queryFn: () => apiRequest<{ settings: Setting[]; schema: Record<string, { type: string; apply_mode: string }> }>("/settings") });
const draft = reactive<Record<string, unknown>>({});
const mutation = useMutation({ mutationFn: ({ item, value }: { item: Setting; value: unknown }) => apiRequest(`/settings`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key: item.key, value, value_version: item.value_version }) }), onSuccess: () => queryClient.invalidateQueries({ queryKey: ["settings"] }) });
const labels: Record<string, string> = { "service.worker.autostart": "Worker 自动启动", "service.worker.start_timeout_seconds": "Worker 启动超时", "checkin.enabled": "启用自动签到", "checkin.at": "签到时间", "checkin.timezone": "签到时区", "monitoring.metrics_interval_seconds": "账号指标刷新间隔", "usage.rollup_interval_seconds": "用量聚合间隔", "usage.detail_retention_days": "请求明细保留天数" };
const groups = computed(() => { const result: Record<string, Setting[]> = {}; for (const item of settings.data.value?.settings ?? []) { const group = item.key.split(".")[0]; (result[group] ??= []).push(item); } return result; });
watch(() => settings.data.value?.settings, (items) => { for (const item of items ?? []) if (!(item.key in draft)) draft[item.key] = item.value; }, { immediate: true });
function save(item: Setting): void { mutation.mutate({ item, value: draft[item.key] }); }
function isBoolean(item: Setting): boolean { return typeof item.value === "boolean"; }
</script>

<template>
  <section class="page-content">
    <header class="page-header"><div><p class="eyebrow">Runtime configuration</p><h1>运行设置</h1><p>修改会经过版本校验，并立即应用到调度器；不支持热应用的设置会明确返回错误。</p></div><div class="security-banner compact"><Settings2 :size="18" /><span>设置来源：SQLite runtime_settings</span></div></header>
    <div v-if="settings.isError.value" class="alert alert--error">设置读取失败：{{ settings.error.value }}</div>
    <section v-for="(items, group) in groups" :key="group" class="data-panel"><PanelHeader :title="group === 'service' ? '服务' : group === 'checkin' ? '签到' : group === 'monitoring' ? '指标' : '用量'" description="每项保存后会记录版本和审计结果"><template #default><CircleHelp :size="17" /></template></PanelHeader><div class="settings-list"><div v-for="item in items" :key="item.key" class="setting-row"><div class="setting-copy"><strong>{{ labels[item.key] ?? item.key }}</strong><small>{{ item.key }} · {{ item.apply_mode }}</small></div><div class="setting-control"><label v-if="isBoolean(item)" class="switch"><input v-model="draft[item.key]" type="checkbox" /><span></span></label><input v-else v-model="draft[item.key]" :type="item.key.endsWith('.at') ? 'time' : 'number'" :min="item.key.includes('retention') ? 1 : 30" /><StatePill :value="item.apply_status" /><button class="icon-button" type="button" title="保存设置" @click="save(item)"><Save :size="16" /></button></div><p v-if="item.last_error" class="form-error">{{ item.last_error }}</p></div></div></section>
    <div v-if="mutation.isSuccess.value" class="toast"><Check :size="16" />设置已应用</div><p v-if="mutation.isError.value" class="form-error">设置应用失败：{{ mutation.error.value }}</p>
  </section>
</template>
