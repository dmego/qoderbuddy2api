<!-- SPDX-License-Identifier: LGPL-3.0-or-later
  Derived from Wei-Shaw/sub2api frontend/src/components/layout/AppSidebar.vue at cb24522.
-->
<script setup lang="ts">
import { ChevronLeft, ChevronRight, X } from "@lucide/vue";

defineProps<{ collapsed: boolean }>();
defineEmits<{ collapse: []; close: [] }>();
</script>

<template>
  <div class="flex min-h-16 items-center gap-3 px-4">
    <span class="grid size-8 shrink-0 place-items-center rounded-lg bg-primary-600 text-sm font-bold text-white" aria-hidden="true">2</span>
    <div v-if="!collapsed" class="min-w-0 flex-1">
      <strong class="block text-sm font-semibold text-gray-900 dark:text-white">2api</strong>
      <span class="block truncate text-xs text-gray-500 dark:text-dark-400">多账号代理控制台</span>
    </div>
    <button class="inline-flex size-11 items-center justify-center rounded-xl text-gray-500 hover:bg-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500 lg:hidden dark:text-dark-300 dark:hover:bg-dark-800" type="button" aria-label="关闭导航" @click="$emit('close')">
      <X :size="18" aria-hidden="true" />
    </button>
    <button class="hidden size-9 items-center justify-center rounded-lg text-gray-500 hover:bg-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500 lg:inline-flex dark:text-dark-300 dark:hover:bg-dark-800" type="button" :aria-label="collapsed ? '展开导航' : '收起导航'" :title="collapsed ? '展开导航' : '收起导航'" @click="$emit('collapse')">
      <ChevronRight v-if="collapsed" :size="17" aria-hidden="true" />
      <ChevronLeft v-else :size="17" aria-hidden="true" />
    </button>
  </div>
</template>
