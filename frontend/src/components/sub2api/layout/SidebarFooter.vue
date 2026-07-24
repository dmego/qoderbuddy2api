<!-- SPDX-License-Identifier: LGPL-3.0-or-later
  Derived from Wei-Shaw/sub2api frontend/src/components/layout/AppSidebar.vue at cb24522.
-->
<script setup lang="ts">
import { DatabaseBackup } from "@lucide/vue";

import ThemeToggle from "./ThemeToggle.vue";

defineProps<{ collapsed: boolean }>();
</script>

<template>
  <div class="flex items-center gap-2 border-t border-gray-200 px-3 py-3 dark:border-dark-700">
    <ThemeToggle />
    <span v-if="!collapsed" class="flex min-w-0 flex-1 items-center gap-2 text-xs text-gray-500 dark:text-dark-400"><DatabaseBackup :size="15" aria-hidden="true" />SQLite · 本地加密</span>
  </div>
</template>
