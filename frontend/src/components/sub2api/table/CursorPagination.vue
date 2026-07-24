<!-- SPDX-License-Identifier: LGPL-3.0-or-later
  Derived from Wei-Shaw/sub2api frontend/src/components/common/Pagination.vue at cb24522.
-->
<script setup lang="ts">
import { ChevronLeft, ChevronRight } from "@lucide/vue";

withDefaults(defineProps<{
  page: number;
  hasPrevious: boolean;
  hasNext: boolean;
  total?: number | null;
}>(), { total: null });
const emit = defineEmits<{ previous: []; next: [] }>();
</script>

<template>
  <div class="flex flex-wrap items-center justify-between gap-3 text-sm text-gray-500 dark:text-dark-400">
    <span>第 {{ page }} 页<template v-if="total !== null"> · 共 {{ total }} 条</template></span>
    <nav class="inline-flex overflow-hidden rounded-lg border border-gray-200 dark:border-dark-600" aria-label="分页">
      <button class="inline-flex min-h-10 items-center gap-1 border-r border-gray-200 px-3 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-dark-600 dark:text-dark-200 dark:hover:bg-dark-700" type="button" :disabled="!hasPrevious" @click="emit('previous')"><ChevronLeft :size="16" aria-hidden="true" />上一页</button>
      <button class="inline-flex min-h-10 items-center gap-1 px-3 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50 dark:text-dark-200 dark:hover:bg-dark-700" type="button" :disabled="!hasNext" @click="emit('next')">下一页<ChevronRight :size="16" aria-hidden="true" /></button>
    </nav>
  </div>
</template>
