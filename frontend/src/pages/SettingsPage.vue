<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { CircleHelp, RefreshCcw, RotateCcw, Save, Settings2 } from "@lucide/vue";
import { computed, reactive, ref, watch } from "vue";

import { apiRequest } from "@/api/client";
import ConfirmDialog from "@/components/ConfirmDialog.vue";
import NotificationRegion from "@/components/NotificationRegion.vue";
import OperationStatus from "@/components/OperationStatus.vue";
import PanelHeader from "@/components/PanelHeader.vue";
import StatePill from "@/components/StatePill.vue";
import { useNotifications } from "@/composables/useNotifications";

type Setting = { key: string; value: unknown; value_version: number; source?: string; apply_mode: string; apply_status: string; restart_required?: boolean; last_error?: string };
type Schema = Record<string, { type: "bool" | "int" | "str"; apply_mode: string; default?: unknown }>;
type Meta = { label: string; description: string; min?: number; max?: number; step?: number; unit?: string };
type SettingSnapshot = Readonly<{ setting: Setting; value: unknown; version: number }>;
type SettingOutcome = { key: string; label: string; status: "succeeded" | "failed"; error?: string; operation_id?: string; restart_required?: boolean; value?: unknown; value_version?: number };

const metadata: Record<string, Meta> = {
  "service.worker.autostart": { label: "代理进程自动启动", description: "管理控制台启动后自动拉起代理进程。" },
  "service.worker.start_timeout_seconds": { label: "代理进程启动超时", description: "等待代理进程完成健康检查的最长时间。", min: 5, max: 300, unit: "秒" },
  "checkin.enabled": { label: "启用自动签到", description: "按下方时区和时间运行每日签到。" },
  "checkin.at": { label: "签到时间", description: "调度器使用的本地时间，格式为 HH:mm。" },
  "checkin.timezone": { label: "签到时区", description: "用于日期边界、已签到判断和下一次运行。" },
  "checkin.catch_up": { label: "错过时间后补跑", description: "仅在补跑窗口内执行未完成账号。" },
  "checkin.catch_up_window_hours": { label: "补跑窗口", description: "超过该窗口不会自动补跑。", min: 0, max: 72, unit: "小时" },
  "checkin.jitter_min_seconds": { label: "补跑最小抖动", description: "错过计划时延迟执行的下界。", min: 0, max: 300, unit: "秒" },
  "checkin.jitter_max_seconds": { label: "补跑最大抖动", description: "必须不小于最小抖动。", min: 0, max: 300, unit: "秒" },
  "checkin.retry_limit": { label: "签到重试次数", description: "仅重试网络、超时及可重试的上游错误。", min: 0, max: 10, unit: "次" },
  "monitoring.metrics_enabled": { label: "启用账号指标采集", description: "关闭后停止定时采集，但保留已记录快照。" },
  "monitoring.metrics_interval_seconds": { label: "账号指标刷新间隔", description: "配额、积分与凭据状态采集频率。", min: 30, max: 86400, unit: "秒" },
  "usage.rollup_interval_seconds": { label: "用量聚合间隔", description: "将请求明细汇总到趋势桶的频率。", min: 10, max: 3600, unit: "秒" },
  "usage.detail_retention_days": { label: "请求明细保留天数", description: "降低该值会缩短可查询的请求明细窗口。", min: 1, max: 3650, unit: "天" },
};

const queryClient = useQueryClient();
const draft = reactive<Record<string, unknown>>({});
const pendingItems = ref<SettingSnapshot[]>([]);
const lastOperation = ref<Record<string, unknown> | null>(null);
const { notifications, notify, dismiss } = useNotifications();
const settings = useQuery({ queryKey: ["settings"], queryFn: () => apiRequest<{ settings: Setting[]; schema: Schema }>("/settings"), staleTime: 60_000 });
const groups = computed(() => { const result: Record<string, Setting[]> = {}; for (const item of settings.data.value?.settings ?? []) (result[item.key.split(".")[0]] ??= []).push(item); return result; });
const dirtyItems = computed(() => (settings.data.value?.settings ?? []).filter((item) => draft[item.key] !== item.value));
const errors = computed(() => Object.fromEntries((settings.data.value?.settings ?? []).map((item) => [item.key, validate(item, draft[item.key])]).filter(([, value]) => value)));
watch(() => settings.data.value?.settings, (items) => { for (const item of items ?? []) draft[item.key] = item.value; }, { immediate: true });

const mutation = useMutation({
  mutationFn: async (items: SettingSnapshot[]) => {
    const results: SettingOutcome[] = [];
    for (const item of items) results.push(await saveSetting(item));
    return results;
  },
  onSuccess: async (results) => {
    const failed = results.filter((item) => item.status === "failed").length;
    const succeeded = results.length - failed;
    const status = failed ? succeeded ? "partial" : "failed" : "succeeded";
    lastOperation.value = { action: "应用运行设置", status, total: results.length, succeeded, failed, items: results };
    notify(failed ? "部分设置应用失败" : "运行设置已应用", { message: `${succeeded} 成功 · ${failed} 失败`, tone: failed ? "warning" : "success", timeout: failed ? 0 : 5000 });
    await queryClient.invalidateQueries({ queryKey: ["settings"] });
    for (const item of results) if (item.status === "failed") draft[item.key] = item.value;
  },
});

function schemaType(item: Setting): string { return settings.data.value?.schema[item.key]?.type ?? typeof item.value; }
async function saveSetting(snapshot: SettingSnapshot): Promise<SettingOutcome> {
  const { setting, value, version } = snapshot;
  const label = metadata[setting.key]?.label ?? setting.key;
  try {
    const result = await apiRequest<Record<string, unknown>>("/settings", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key: setting.key, value, value_version: version }) });
    if (result.apply_status === "failed") return { key: setting.key, label, status: "failed", value, error: String(result.error_code ?? "setting_apply_failed"), value_version: Number(result.value_version) };
    return { key: setting.key, label, status: "succeeded", value, value_version: Number(result.value_version), operation_id: typeof result.operation_id === "string" ? result.operation_id : undefined, restart_required: result.restart_required === true };
  } catch (error) {
    return { key: setting.key, label, status: "failed", value, error: error instanceof Error ? error.message : String(error) };
  }
}
function requestSave(items: Setting[]): void { if (!items.length || items.some((item) => errors.value[item.key])) return; const snapshots = items.map(settingSnapshot); const risky = snapshots.some(({ setting, value }) => setting.key === "usage.detail_retention_days" && Number(value) < Number(setting.value)); if (risky) pendingItems.value = snapshots; else mutation.mutate(snapshots); }
function confirmSave(): void { const items = pendingItems.value; pendingItems.value = []; mutation.mutate(items); }
function settingSnapshot(setting: Setting): SettingSnapshot { return Object.freeze({ setting, value: draft[setting.key], version: setting.value_version }); }
function resetDraft(): void { for (const item of settings.data.value?.settings ?? []) draft[item.key] = item.value; }
function validate(item: Setting, value: unknown): string { const type = schemaType(item); if (type === "int" && (typeof value !== "number" || !Number.isInteger(value))) return "请输入整数"; const meta = metadata[item.key]; if (type === "int" && meta?.min !== undefined && Number(value) < meta.min) return `不能小于 ${meta.min}`; if (type === "int" && meta?.max !== undefined && Number(value) > meta.max) return `不能大于 ${meta.max}`; if (item.key === "checkin.at" && !/^([01]\d|2[0-3]):[0-5]\d$/.test(String(value))) return "请输入有效时间"; if (type === "str" && !String(value).trim()) return "不能为空"; return ""; }
function groupLabel(group: string): string { return ({ service: "服务", checkin: "签到", monitoring: "指标", usage: "用量" } as Record<string, string>)[group] ?? group; }
</script>

<template>
  <section class="page-content">
    <header class="page-header"><div><h1>运行设置</h1><p>按配置版本更新 SQLite 运行配置，并明确展示热应用、调度重排与失败状态。</p></div><div class="header-actions"><button class="secondary-button" type="button" :disabled="settings.isFetching.value" @click="settings.refetch()"><RefreshCcw :class="{ spin: settings.isFetching.value }" :size="16" />刷新</button><button class="secondary-button" type="button" :disabled="!dirtyItems.length || mutation.isPending.value" @click="resetDraft"><RotateCcw :size="16" />放弃更改</button><button type="button" :disabled="!dirtyItems.length || Object.keys(errors).length > 0 || mutation.isPending.value" @click="requestSave(dirtyItems)"><Save :size="16" />保存全部（{{ dirtyItems.length }}）</button></div></header>
    <div class="security-banner compact"><Settings2 :size="18" /><span>来源：SQLite 运行设置表 · 版本冲突不会覆盖其他管理员的新值</span></div>
    <div v-if="settings.isPending.value" class="loading-row">正在读取运行设置…</div><div v-else-if="settings.isError.value" class="data-state data-state--error" role="alert">设置读取失败：{{ settings.error.value }}<button class="secondary-button compact-button" type="button" @click="settings.refetch()">重试</button></div><div v-else-if="!settings.data.value?.settings.length" class="empty-state"><strong>没有可管理的运行设置</strong><span>检查管理控制台的设置结构是否已加载。</span></div>
    <div v-if="settings.isStale.value && settings.data.value" class="data-state data-state--warning">当前设置快照可能已过期，保存前建议刷新以减少版本冲突。</div>

    <section v-for="(items, group) in groups" :key="group" class="data-panel"><PanelHeader :title="groupLabel(String(group))" description="每项保存后都会记录配置版本、应用方式与审计结果。"><CircleHelp :size="17" /></PanelHeader><div class="settings-list"><div v-for="item in items" :key="item.key" class="setting-row" :class="{ 'setting-row--dirty': draft[item.key] !== item.value }"><div class="setting-copy"><strong>{{ metadata[item.key]?.label ?? item.key }}</strong><span>{{ metadata[item.key]?.description }}</span><small>{{ item.key }} · v{{ item.value_version }} · {{ item.source ?? "默认值" }} · {{ item.apply_mode }}</small><small v-if="item.restart_required">需要重启管理控制台后生效</small></div><div class="setting-control"><label v-if="schemaType(item) === 'bool'" class="switch" :aria-label="metadata[item.key]?.label ?? item.key"><input v-model="draft[item.key]" type="checkbox" :disabled="mutation.isPending.value" /><span></span></label><input v-else-if="item.key === 'checkin.at'" v-model="draft[item.key]" :aria-label="metadata[item.key]?.label ?? item.key" type="time" :disabled="mutation.isPending.value" /><select v-else-if="item.key === 'checkin.timezone'" v-model="draft[item.key]" :aria-label="metadata[item.key]?.label ?? item.key" :disabled="mutation.isPending.value"><option value="Asia/Shanghai">Asia/Shanghai</option><option value="UTC">UTC</option><option value="America/Los_Angeles">America/Los_Angeles</option><option value="Europe/London">Europe/London</option></select><div v-else class="input-with-unit"><input v-model.number="draft[item.key]" :aria-label="metadata[item.key]?.label ?? item.key" type="number" :min="metadata[item.key]?.min" :max="metadata[item.key]?.max" :step="metadata[item.key]?.step ?? 1" :disabled="mutation.isPending.value" /><span>{{ metadata[item.key]?.unit }}</span></div><StatePill :value="item.apply_status" /><button class="icon-button" type="button" :aria-label="`保存 ${metadata[item.key]?.label ?? item.key}`" :title="`保存 ${metadata[item.key]?.label ?? item.key}`" :disabled="draft[item.key] === item.value || Boolean(errors[item.key]) || mutation.isPending.value" @click="requestSave([item])"><Save :size="16" /></button></div><p v-if="errors[item.key]" class="form-error" role="alert">{{ errors[item.key] }}</p><p v-if="item.last_error" class="form-error">最近应用错误：{{ item.last_error }}</p></div></div></section>

    <OperationStatus :operation="lastOperation" />
    <ConfirmDialog :open="pendingItems.length > 0" title="缩短请求明细保留时间？" description="降低保留天数会使超出新窗口的历史请求明细进入清理范围；聚合统计和审计记录不受影响。" confirm-label="确认保存" tone="danger" :verification-text="'RETENTION'" :busy="mutation.isPending.value" @cancel="pendingItems = []" @confirm="confirmSave" />
    <NotificationRegion :notifications="notifications" @dismiss="dismiss" />
  </section>
</template>
