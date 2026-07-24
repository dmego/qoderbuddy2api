<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { Activity, Play, RefreshCcw, RotateCw, Square } from "@lucide/vue";
import { computed, ref } from "vue";

import PanelHeader from "@/components/PanelHeader.vue";
import StatePill from "@/components/StatePill.vue";
import { apiRequest, pollOperation } from "@/api/client";

type ServiceState = {
  service: string;
  desired_state: string;
  observed_state: string;
  identity?: { pid: number; process_start_time: number; owner_instance_id: string };
  in_flight: number;
  last_error?: string;
  last_exit_code?: number;
};

const queryClient = useQueryClient();
const recent = ref<Record<string, unknown>[]>([]);
const service = useQuery({
  queryKey: ["service"],
  queryFn: () => apiRequest<ServiceState>("/service"),
  refetchInterval: 3000,
});
const action = useMutation({
  mutationFn: async (name: string) => {
    const operation = await apiRequest<Record<string, unknown>>(`/service/${name}`, { method: "POST" });
    if (operation.status === "running") return pollOperation(String(operation.operation_id));
    return operation;
  },
  onSuccess: async (result) => {
    recent.value = [result, ...recent.value].slice(0, 8);
    await queryClient.invalidateQueries({ queryKey: ["service"] });
  },
});
const isRunning = computed(() => service.data.value?.observed_state === "HEALTHY");

function run(name: string): void { action.mutate(name); }
</script>

<template>
  <section class="page-content">
    <header class="page-header">
      <div><p class="eyebrow">Process control</p><h1>代理服务</h1><p>独立管理 Proxy Worker 的启动、停止、重启与配置重载。</p></div>
      <button class="secondary-button" type="button" @click="service.refetch()"><RefreshCcw :size="16" />刷新</button>
    </header>

    <div v-if="service.isError.value" class="alert alert--error">无法读取服务状态：{{ service.error.value }}</div>
    <div class="service-layout">
      <section class="data-panel service-console">
        <PanelHeader title="Worker 生命周期" description="Control Plane 在 Worker 停止时仍保持可用">
          <StatePill :value="service.data.value?.observed_state" />
        </PanelHeader>
        <div class="service-status-block">
          <div><span>期望状态</span><strong>{{ service.data.value?.desired_state ?? "--" }}</strong></div>
          <div><span>观测状态</span><strong>{{ service.data.value?.observed_state ?? "加载中" }}</strong></div>
          <div><span>进程 PID</span><strong>{{ service.data.value?.identity?.pid ?? "--" }}</strong></div>
          <div><span>活动请求</span><strong>{{ service.data.value?.in_flight ?? 0 }}</strong></div>
        </div>
        <div class="command-bar" aria-label="服务操作">
          <button type="button" :disabled="action.isPending.value || isRunning" @click="run('start')"><Play :size="16" />启动</button>
          <button type="button" class="danger-button" :disabled="action.isPending.value || !isRunning" @click="run('stop')"><Square :size="15" />停止</button>
          <button type="button" :disabled="action.isPending.value" @click="run('restart')"><RotateCw :size="16" />重启</button>
          <button type="button" :disabled="action.isPending.value" @click="run('reload')"><Activity :size="16" />重载</button>
        </div>
        <p v-if="action.isError.value" class="form-error">{{ action.error.value }}</p>
      </section>

      <aside class="data-panel diagnostic-panel">
        <PanelHeader title="进程诊断" description="只显示经过身份校验的 Worker 信息" />
        <dl class="detail-list">
          <div><dt>Owner</dt><dd>{{ service.data.value?.identity?.owner_instance_id ?? "未分配" }}</dd></div>
          <div><dt>启动时间</dt><dd>{{ service.data.value?.identity?.process_start_time ?? "--" }}</dd></div>
          <div><dt>退出码</dt><dd>{{ service.data.value?.last_exit_code ?? "--" }}</dd></div>
          <div><dt>最近错误</dt><dd :class="{ 'text-danger': service.data.value?.last_error }">{{ service.data.value?.last_error ?? "无" }}</dd></div>
        </dl>
      </aside>
    </div>

    <section class="data-panel">
      <PanelHeader title="本次会话操作" description="启动、停止和重载结果会保留在数据库审计记录中" />
      <div v-if="!recent.length" class="compact-empty">尚未执行服务操作</div>
      <div v-else class="table-wrap"><table><thead><tr><th>操作</th><th>状态</th><th>Operation ID</th><th>错误</th></tr></thead><tbody><tr v-for="item in recent" :key="String(item.operation_id)"><td>{{ item.action }}</td><td><StatePill :value="String(item.status)" /></td><td class="mono">{{ item.operation_id }}</td><td>{{ item.error ?? "--" }}</td></tr></tbody></table></div>
    </section>
  </section>
</template>
