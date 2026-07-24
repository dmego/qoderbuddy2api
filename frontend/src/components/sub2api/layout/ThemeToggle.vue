<!-- SPDX-License-Identifier: LGPL-3.0-or-later
  Derived from Wei-Shaw/sub2api frontend layout theme controls at cb24522.
-->
<script setup lang="ts">
import { Monitor, Moon, Sun } from "@lucide/vue";
import { computed } from "vue";

import { type Theme, useUiStore } from "@/stores/ui";

const ui = useUiStore();
const nextTheme: Record<Theme, Theme> = { system: "light", light: "dark", dark: "system" };
const metadata = computed(() => ({
  system: { label: "跟随系统主题", icon: Monitor },
  light: { label: "浅色主题", icon: Sun },
  dark: { label: "深色主题", icon: Moon },
}[ui.theme]));

function cycleTheme(): void {
  ui.setTheme(nextTheme[ui.theme]);
}
</script>

<template>
  <button
    class="inline-flex size-11 items-center justify-center rounded-xl text-gray-600 transition-colors hover:bg-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500 focus-visible:ring-offset-2 dark:text-dark-300 dark:hover:bg-dark-800 dark:focus-visible:ring-offset-dark-900"
    type="button"
    :aria-label="`切换主题，当前：${metadata.label}`"
    :title="`切换主题，当前：${metadata.label}`"
    @click="cycleTheme"
  >
    <component :is="metadata.icon" :size="18" aria-hidden="true" />
  </button>
</template>
