<!-- SPDX-License-Identifier: LGPL-3.0-or-later
  Derived from Wei-Shaw/sub2api frontend/src/components/layout/AppSidebar.vue at cb24522.
-->
<script setup lang="ts">
import {
  BookKey, Boxes, ChartNoAxesCombined, CheckCircle2, CircleGauge, ClipboardList, KeyRound, Power, Settings, UsersRound,
} from "@lucide/vue";
import { RouterLink } from "vue-router";

import SidebarBrand from "./SidebarBrand.vue";
import SidebarFooter from "./SidebarFooter.vue";

type NavItem = { name: string; label: string; icon: unknown };
type NavGroup = { label: string; items: NavItem[] };

defineProps<{ collapsed: boolean; open: boolean }>();
const emit = defineEmits<{ collapse: []; close: []; navigate: [] }>();
const groups: NavGroup[] = [
  { label: "运行", items: [{ name: "overview", label: "总览", icon: CircleGauge }, { name: "service", label: "代理服务", icon: Power }] },
  { label: "账号池", items: [{ name: "accounts", label: "账号", icon: UsersRound }, { name: "credentials", label: "凭据", icon: BookKey }] },
  { label: "代理与模型", items: [{ name: "proxy-keys", label: "代理密钥", icon: KeyRound }, { name: "models", label: "模型", icon: Boxes }, { name: "usage", label: "用量", icon: ChartNoAxesCombined }] },
  { label: "自动化", items: [{ name: "checkin", label: "签到", icon: CheckCircle2 }] },
  { label: "治理", items: [{ name: "settings", label: "设置", icon: Settings }, { name: "audit", label: "审计", icon: ClipboardList }] },
];
</script>

<template>
  <aside
    data-testid="admin-sidebar"
    class="fixed inset-y-0 left-0 z-40 flex w-[min(16rem,calc(100vw-3rem))] -translate-x-full flex-col border-r border-gray-200 bg-white transition-[width,transform] duration-300 lg:translate-x-0 dark:border-dark-700 dark:bg-dark-900"
    :class="[collapsed ? 'lg:w-[72px]' : 'lg:w-64', open ? 'translate-x-0 shadow-glass' : '']"
    aria-label="主导航"
  >
    <SidebarBrand :collapsed="collapsed" @collapse="emit('collapse')" @close="emit('close')" />
    <nav id="control-navigation" class="min-h-0 flex-1 space-y-6 overflow-y-auto px-3 py-4">
      <section v-for="group in groups" :key="group.label" class="space-y-1">
        <h2 v-if="!collapsed" class="px-3 text-xs font-medium text-gray-400 dark:text-dark-500">{{ group.label }}</h2>
        <RouterLink
          v-for="item in group.items"
          :key="item.name"
          :to="{ name: item.name }"
          class="flex min-h-11 items-center gap-3 rounded-xl px-3 text-sm font-medium text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500 dark:text-dark-300 dark:hover:bg-dark-800 dark:hover:text-white"
          active-class="bg-primary-50 text-primary-700 dark:bg-primary-900/30 dark:text-primary-300"
          :title="collapsed ? item.label : undefined"
          @click="emit('navigate')"
        >
          <component :is="item.icon" class="shrink-0" :size="18" aria-hidden="true" />
          <span v-if="!collapsed">{{ item.label }}</span>
        </RouterLink>
      </section>
    </nav>
    <SidebarFooter :collapsed="collapsed" />
  </aside>
</template>
