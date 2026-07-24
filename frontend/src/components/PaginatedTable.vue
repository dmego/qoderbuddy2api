<script setup lang="ts">
import { ChevronLeft, ChevronRight, LoaderCircle, RefreshCcw } from "@lucide/vue";
import { useAttrs } from "vue";

withDefaults(defineProps<{
  loading?: boolean;
  error?: string;
  empty?: boolean;
  emptyTitle?: string;
  emptyDescription?: string;
  stale?: boolean;
  unavailable?: boolean;
  page?: number;
  pageSize?: number;
  total?: number | null;
  canPrevious?: boolean;
  canNext?: boolean;
}>(), {
  loading: false,
  error: "",
  empty: false,
  emptyTitle: "暂无数据",
  emptyDescription: "调整筛选条件或稍后重试。",
  stale: false,
  unavailable: false,
  page: 1,
  pageSize: 20,
  total: null,
  canPrevious: false,
  canNext: false,
});
defineEmits<{ retry: []; previous: []; next: [] }>();
const attrs = useAttrs();
</script>

<template>
  <div class="paginated-table" :aria-busy="loading">
    <div v-if="stale && !loading" class="data-state data-state--warning" role="status">当前显示缓存快照，后台刷新尚未完成。</div>
    <div v-if="unavailable" class="data-state data-state--warning" role="status">此数据源暂不可用，已保留其他可用信息。</div>
    <div v-if="loading" class="loading-row"><LoaderCircle class="spin" :size="18" />正在加载数据…</div>
    <div v-else-if="error" class="data-state data-state--error" role="alert"><span>{{ error }}</span><button class="secondary-button compact-button" type="button" @click="$emit('retry')"><RefreshCcw :size="14" />重试</button></div>
    <div v-else-if="empty" class="empty-state"><slot name="empty"><strong>{{ emptyTitle }}</strong><span>{{ emptyDescription }}</span></slot></div>
    <div v-else class="table-wrap">
      <table><caption class="sr-only">{{ attrs['aria-label'] ?? '数据表格' }}</caption><thead><slot name="header" /></thead><tbody><slot /></tbody></table>
    </div>
    <footer v-if="!loading && !error && !empty" class="pagination-bar">
      <span>第 {{ page }} 页<template v-if="total !== null"> · 共 {{ total }} 条</template><template v-else> · 每页最多 {{ pageSize }} 条</template></span>
      <div><button class="secondary-button compact-button" type="button" :disabled="!canPrevious" @click="$emit('previous')"><ChevronLeft :size="15" />上一页</button><button class="secondary-button compact-button" type="button" :disabled="!canNext" @click="$emit('next')">下一页<ChevronRight :size="15" /></button></div>
    </footer>
  </div>
</template>
