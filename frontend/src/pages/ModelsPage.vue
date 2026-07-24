<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { Boxes, Check, RefreshCcw, SlidersHorizontal } from "@lucide/vue";

import PanelHeader from "@/components/PanelHeader.vue";
import StatePill from "@/components/StatePill.vue";
import { apiRequest } from "@/api/client";

type Model = { provider: string; model_id: string; display_name: string; capabilities: string[]; source: string; enabled: boolean; last_seen_at: string };
const queryClient = useQueryClient();
const models = useQuery({ queryKey: ["models"], queryFn: () => apiRequest<{ models: Model[] }>("/models") });
const refresh = useMutation({ mutationFn: () => apiRequest("/models/refresh", { method: "POST" }), onSuccess: () => queryClient.invalidateQueries({ queryKey: ["models"] }) });
const toggle = useMutation({ mutationFn: (model: Model) => apiRequest(`/models/${model.provider}/${encodeURIComponent(model.model_id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: !model.enabled }) }), onSuccess: () => queryClient.invalidateQueries({ queryKey: ["models"] }) });
</script>

<template>
  <section class="page-content">
    <header class="page-header"><div><p class="eyebrow">Provider catalogue</p><h1>模型管理</h1><p>控制每个 Provider 的模型可见性、能力标签和最近发现时间。</p></div><div class="header-actions"><button class="secondary-button" type="button" @click="models.refetch()"><RefreshCcw :size="16" />读取目录</button><button type="button" :disabled="refresh.isPending.value" @click="refresh.mutate()"><Boxes :size="16" />刷新模型目录</button></div></header>
    <section class="data-panel"><PanelHeader title="模型目录" :description="`${models.data.value?.models.length ?? 0} 个模型 · 变更会记录审计事件`"><template #default><SlidersHorizontal :size="17" /></template></PanelHeader><div v-if="models.isPending.value" class="loading-row">正在加载模型目录…</div><div v-else-if="models.isError.value" class="alert alert--error">模型目录读取失败：{{ models.error.value }}</div><div v-else-if="!models.data.value?.models.length" class="empty-state"><Boxes :size="28" /><strong>目录为空</strong><span>点击“刷新模型目录”从当前配置导入模型定义。</span></div><div v-else class="table-wrap"><table><thead><tr><th>模型</th><th>Provider</th><th>能力</th><th>来源</th><th>最近发现</th><th>状态</th><th></th></tr></thead><tbody><tr v-for="model in models.data.value.models" :key="`${model.provider}:${model.model_id}`"><td><strong>{{ model.display_name || model.model_id }}</strong><small class="mono">{{ model.model_id }}</small></td><td><span class="provider-mark" :class="`provider-mark--${model.provider}`">{{ model.provider }}</span></td><td><div class="tag-list"><span v-for="capability in model.capabilities" :key="capability">{{ capability }}</span></div></td><td>{{ model.source }}</td><td>{{ model.last_seen_at }}</td><td><StatePill :value="model.enabled" /></td><td><button class="icon-button" :title="model.enabled ? '停用模型' : '启用模型'" type="button" @click="toggle.mutate(model)"><Check :size="16" /></button></td></tr></tbody></table></div></section>
  </section>
</template>
