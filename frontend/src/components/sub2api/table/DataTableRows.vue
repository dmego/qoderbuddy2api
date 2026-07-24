<!-- SPDX-License-Identifier: LGPL-3.0-or-later
  Derived from Wei-Shaw/sub2api frontend/src/components/common/DataTable.vue at cb24522.
-->
<script setup lang="ts">
withDefaults(defineProps<{
  count: number;
  loading?: boolean;
  emptyTitle?: string;
  emptyDescription?: string;
  colspan: number;
}>(), {
  loading: false,
  emptyTitle: "暂无数据",
  emptyDescription: "调整筛选条件或稍后重试。",
});
</script>

<template>
  <tr v-if="loading"><td :colspan="colspan" class="px-5 py-12 text-center text-sm text-gray-500 dark:text-dark-400">正在加载数据…</td></tr>
  <slot v-else-if="count > 0" />
  <tr v-else><td :colspan="colspan" class="px-5 py-12 text-center"><strong class="block text-sm text-gray-800 dark:text-white">{{ emptyTitle }}</strong><span class="mt-1 block text-sm text-gray-500 dark:text-dark-400">{{ emptyDescription }}</span></td></tr>
</template>
