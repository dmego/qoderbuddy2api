<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { Activity, Play, RefreshCcw, RotateCw, Square } from "@lucide/vue";
import { computed, ref } from "vue";

import { apiRequest, pollOperation } from "@/api/client";
import ConfirmDialog from "@/components/ConfirmDialog.vue";
import NotificationRegion from "@/components/NotificationRegion.vue";
import OperationStatus from "@/components/OperationStatus.vue";
import PaginatedTable from "@/components/PaginatedTable.vue";
import PanelHeader from "@/components/PanelHeader.vue";
import StatePill from "@/components/StatePill.vue";
import { appendQuery, useCursorPager } from "@/composables/useCursorPager";
import { useNotifications } from "@/composables/useNotifications";
import { statusLabel } from "@/utils/presentation";

type ServiceState = {
  service: string; desired_state: string; observed_state: string; in_flight: number;
  identity?: { pid: number; process_start_time: number; owner_instance_id: string };
  started_at?: number; stopped_at?: number; last_health_at?: number; last_error?: string;
  last_exit_code?: number; runtime_snapshot_version?: number;
};
type ServiceEvent = {
  event_id: string; event_type?: string; action?: string; status?: string;
  desired_state?: string; observed_state?: string; in_flight?: number; operation_id?: string;
  error_code?: string; created_at: string | number;
};
type EventPage = { events: ServiceEvent[]; next_cursor?: string | null };

const queryClient = useQueryClient();
const eventType = ref("");
const eventStatus = ref("");
const recent = ref<Record<string, unknown>[]>([]);
const activeOperation = ref<Record<string, unknown> | null>(null);
const confirmAction = ref<"stop" | "restart" | null>(null);
const { cursor, page, canPrevious, next, previous, reset } = useCursorPager();
const { notifications, notify, dismiss } = useNotifications();

const service = useQuery({ queryKey: ["service"], queryFn: () => apiRequest<ServiceState>("/service"), refetchInterval: 3000 });
const events = useQuery({
  queryKey: ["service-events", cursor, eventType, eventStatus],
  queryFn: () => apiRequest<EventPage>(appendQuery("/service/events", { limit: 20, cursor: cursor.value, event_type: eventType.value, status: eventStatus.value })),
  refetchInterval: 5000,
  staleTime: 10_000,
});
const action = useMutation({
  mutationFn: async (name: string) => {
    const result = await apiRequest<Record<string, unknown>>(`/service/${name}`, { method: "POST", headers: { "Idempotency-Key": operationKey(name) } });
    activeOperation.value = result;
    return result.status === "running" ? pollOperation(String(result.operation_id)) : result;
  },
  onSuccess: async (result) => {
    activeOperation.value = result;
    recent.value = [result, ...recent.value].slice(0, 6);
    notifyTerminal(result);
    await Promise.all([queryClient.invalidateQueries({ queryKey: ["service"] }), queryClient.invalidateQueries({ queryKey: ["service-events"] })]);
  },
  onError: (error) => notify("服务操作失败", { message: String(error), tone: "error", timeout: 0 }),
});

const isRunning = computed(() => service.data.value?.observed_state === "HEALTHY");
const isDraining = computed(() => Boolean(service.data.value?.in_flight) && ["STOPPING", "STOPPED"].includes(service.data.value?.observed_state ?? ""));

function requestAction(name: "start" | "stop" | "restart" | "reload"): void {
  if (name === "stop" || name === "restart") confirmAction.value = name;
  else action.mutate(name);
}
function confirmServiceAction(): void { if (confirmAction.value) action.mutate(confirmAction.value); confirmAction.value = null; }
function notifyTerminal(result: Record<string, unknown>): void {
  const status = String(result.status ?? "unknown");
  const message = String(result.error ?? result.error_code ?? result.action ?? "状态已更新");
  if (status === "succeeded") notify("服务操作已完成", { message, tone: "success" });
  else if (status === "failed") notify("服务操作失败", { message, tone: "error", timeout: 0 });
  else notify(status === "cancelled" ? "服务操作已取消" : "服务操作状态异常", { message, tone: "warning", timeout: 0 });
}
function resetEventFilters(): void { eventType.value = ""; eventStatus.value = ""; reset(); }
function operationKey(actionName: string): string { return `${actionName}-${Date.now()}-${Math.random().toString(16).slice(2)}`; }
function displayTime(value?: string | number): string { if (!value) return "--"; const date = new Date(typeof value === "number" && value < 10_000_000_000 ? value * 1000 : value); return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString(); }
</script>

<template>
  <section class="page-content">
    <header class="page-header">
      <div><h1>代理服务</h1><p>管理代理进程生命周期，并核对持久化事件、排空进度与错误边界。</p></div>
      <button class="secondary-button" type="button" :disabled="service.isFetching.value" @click="service.refetch()"><RefreshCcw :class="{ spin: service.isFetching.value }" :size="16" />刷新状态</button>
    </header>

    <div v-if="service.isError.value" class="data-state data-state--error" role="alert">无法读取服务状态：{{ service.error.value }}<button class="secondary-button compact-button" type="button" @click="service.refetch()">重试</button></div>
    <div class="service-layout">
      <section class="data-panel service-console">
        <PanelHeader title="代理进程生命周期" description="管理控制台独立存活；停止前会先观察活动请求排空。"><StatePill :value="service.data.value?.observed_state" /></PanelHeader>
        <div class="service-status-block" :class="{ 'skeleton-block': service.isPending.value }">
          <div><span>期望状态</span><strong>{{ statusLabel(service.data.value?.desired_state) }}</strong></div>
          <div><span>观测状态</span><strong>{{ statusLabel(service.data.value?.observed_state) }}</strong></div>
          <div><span>活动请求</span><strong>{{ service.data.value?.in_flight ?? "--" }}</strong><small>{{ isDraining ? "正在排空" : "实时采样" }}</small></div>
          <div><span>运行快照版本</span><strong>{{ service.data.value?.runtime_snapshot_version ?? "--" }}</strong></div>
        </div>
        <div v-if="isDraining" class="data-state data-state--warning"><Activity :size="17" />代理进程正在排空 {{ service.data.value?.in_flight }} 个活动请求，请勿重复停止。</div>
        <div class="command-bar" aria-label="服务操作">
          <button type="button" :disabled="action.isPending.value || isRunning" @click="requestAction('start')"><Play :size="16" />启动</button>
          <button class="danger-button" type="button" :disabled="action.isPending.value || !isRunning" @click="requestAction('stop')"><Square :size="15" />停止</button>
          <button type="button" :disabled="action.isPending.value" @click="requestAction('restart')"><RotateCw :size="16" />重启</button>
          <button class="secondary-button" type="button" :disabled="action.isPending.value || !isRunning" @click="requestAction('reload')"><Activity :size="16" />重载配置</button>
        </div>
        <OperationStatus :operation="activeOperation" title="服务操作" />
      </section>

      <aside class="data-panel diagnostic-panel">
        <PanelHeader title="进程诊断" description="仅显示经过身份校验的代理进程元数据。" />
        <dl class="detail-list">
          <div><dt>PID / Owner</dt><dd>{{ service.data.value?.identity?.pid ?? "--" }} · {{ service.data.value?.identity?.owner_instance_id ?? "未分配" }}</dd></div>
          <div><dt>启动时间</dt><dd>{{ displayTime(service.data.value?.started_at ?? service.data.value?.identity?.process_start_time) }}</dd></div>
          <div><dt>最后健康检查</dt><dd>{{ displayTime(service.data.value?.last_health_at) }}</dd></div>
          <div><dt>退出码</dt><dd>{{ service.data.value?.last_exit_code ?? "--" }}</dd></div>
          <div><dt>最近错误</dt><dd :class="{ 'text-danger': service.data.value?.last_error }">{{ service.data.value?.last_error ?? "无" }}</dd></div>
        </dl>
      </aside>
    </div>

    <section class="data-panel">
      <PanelHeader title="本次会话操作" description="异步操作完成后会保留最终状态；完整历史以持久化事件为准。" />
      <div v-if="!recent.length" class="compact-empty">本次会话尚未执行服务操作。</div>
      <div v-else class="operation-grid"><OperationStatus v-for="item in recent" :key="String(item.operation_id)" :operation="item" /></div>
    </section>

    <section class="data-panel">
      <PanelHeader title="持久化事件" :description="`第 ${page} 页 · 代理进程重启后仍可追溯`">
        <div class="toolbar"><select v-model="eventType" aria-label="筛选事件类型" @change="reset"><option value="">全部事件</option><option value="state">状态快照</option><option value="operation">管理操作</option></select><select v-model="eventStatus" aria-label="筛选事件状态" @change="reset"><option value="">全部状态</option><option value="succeeded">成功</option><option value="failed">失败</option><option value="running">运行中</option></select><button class="secondary-button compact-button" type="button" @click="resetEventFilters">清除</button></div>
      </PanelHeader>
      <PaginatedTable aria-label="服务持久化事件" :loading="events.isPending.value" :error="events.isError.value ? `事件读取失败：${events.error.value}` : ''" :empty="!(events.data.value?.events.length)" empty-title="暂无服务事件" empty-description="执行生命周期操作后，事件会持久化到这里。" :stale="events.isStale.value" :page="page" :can-previous="canPrevious.length > 0" :can-next="Boolean(events.data.value?.next_cursor)" @retry="events.refetch()" @previous="previous" @next="next(events.data.value?.next_cursor)">
        <template #header><tr><th>时间</th><th>事件 / 操作</th><th>状态迁移</th><th>活动请求</th><th>结果</th><th>错误</th></tr></template>
        <tr v-for="event in events.data.value?.events ?? []" :key="event.event_id"><td><strong>{{ displayTime(event.created_at) }}</strong><small class="mono">{{ event.event_id }}</small></td><td>{{ event.event_type ?? "服务" }}<small>{{ event.action ?? event.operation_id ?? "--" }}</small></td><td>{{ statusLabel(event.desired_state) }} / {{ statusLabel(event.observed_state) }}</td><td>{{ event.in_flight ?? "--" }}</td><td><StatePill :value="event.status" /></td><td :class="{ 'text-danger': event.error_code }">{{ event.error_code ?? "--" }}</td></tr>
      </PaginatedTable>
    </section>

    <ConfirmDialog :open="Boolean(confirmAction)" :title="confirmAction === 'stop' ? '停止代理服务？' : '重启代理服务？'" :description="confirmAction === 'stop' ? '新的代理请求将不可用；系统会观察活动请求排空后停止。' : '重启会短暂中断新请求，并重新加载当前运行配置。'" :confirm-label="confirmAction === 'stop' ? '确认停止' : '确认重启'" tone="danger" :busy="action.isPending.value" @cancel="confirmAction = null" @confirm="confirmServiceAction" />
    <NotificationRegion :notifications="notifications" @dismiss="dismiss" />
  </section>
</template>
