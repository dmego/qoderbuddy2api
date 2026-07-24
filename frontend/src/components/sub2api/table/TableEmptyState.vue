<!-- SPDX-License-Identifier: LGPL-3.0-or-later
  Derived from Wei-Shaw/sub2api frontend/src/components/common/DataTable.vue at cb24522.
-->
<script setup lang="ts">
withDefaults(defineProps<{ title?: string; description?: string }>(), {
  title: "暂无数据",
  description: "调整筛选条件或稍后重试。",
});
</script>

<template>
  <div class="flex min-h-48 flex-col items-center justify-center px-6 py-12 text-center"><strong class="text-sm text-gray-800 dark:text-white">{{ title }}</strong><p class="mt-1 text-sm text-gray-500 dark:text-dark-400">{{ description }}</p><slot /></div>
</template>
