<!-- SPDX-License-Identifier: LGPL-3.0-or-later
  Derived from Wei-Shaw/sub2api frontend/src/components/layout/AppHeader.vue at cb24522.
-->
<script setup lang="ts">
import { Activity, LogOut, Menu, X } from "@lucide/vue";
import { useQuery } from "@tanstack/vue-query";
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";

import { apiRequest } from "@/api/client";
import { useSessionStore } from "@/stores/session";
import { statusLabel } from "@/utils/presentation";

import { routeTitle } from "./routeTitles";

const props = defineProps<{ mobileNavigationOpen: boolean }>();
const emit = defineEmits<{ toggleNavigation: [] }>();
const route = useRoute();
const router = useRouter();
const session = useSessionStore();
const service = useQuery({ queryKey: ["service"], queryFn: () => apiRequest<{ observed_state: string; in_flight: number }>("/service"), refetchInterval: 3000 });
const title = computed(() => routeTitle(route.name));

async function logout(): Promise<void> {
  await apiRequest("/session/logout", { method: "POST" });
  session.clear();
  await router.replace("/login");
}
</script>

<template>
  <header class="sticky top-0 z-30 flex min-h-16 items-center justify-between border-b border-gray-200 bg-white/80 px-4 backdrop-blur-xl md:px-6 dark:border-dark-700 dark:bg-dark-900/80">
    <div class="flex min-w-0 items-center gap-3">
      <button class="inline-flex size-11 items-center justify-center rounded-xl text-gray-600 hover:bg-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500 lg:hidden dark:text-dark-300 dark:hover:bg-dark-800" type="button" :aria-label="props.mobileNavigationOpen ? '收起导航' : '展开导航'" :aria-expanded="props.mobileNavigationOpen" aria-controls="control-navigation" @click="emit('toggleNavigation')">
        <X v-if="props.mobileNavigationOpen" :size="19" aria-hidden="true" />
        <Menu v-else :size="19" aria-hidden="true" />
      </button>
      <div class="min-w-0"><p class="truncate text-base font-semibold text-gray-900 dark:text-white">{{ title }}</p><p class="hidden text-xs text-gray-500 sm:block dark:text-dark-400">多账号代理服务管理</p></div>
    </div>
    <div class="flex items-center gap-2 sm:gap-4">
      <div class="hidden items-center gap-2 text-xs text-gray-600 sm:flex dark:text-dark-300" role="status"><Activity :size="16" aria-hidden="true" /><span>代理 {{ statusLabel(service.data.value?.observed_state ?? "unknown") }}</span><strong class="font-semibold text-gray-900 dark:text-white">{{ service.data.value?.in_flight ?? 0 }}</strong></div>
      <button class="inline-flex size-11 items-center justify-center rounded-xl text-gray-600 hover:bg-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500 dark:text-dark-300 dark:hover:bg-dark-800" type="button" aria-label="退出登录" title="退出登录" @click="logout"><LogOut :size="18" aria-hidden="true" /></button>
    </div>
  </header>
</template>
