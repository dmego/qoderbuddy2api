<!-- SPDX-License-Identifier: LGPL-3.0-or-later
  Derived from Wei-Shaw/sub2api frontend/src/components/layout/AppLayout.vue at cb24522.
-->
<script setup lang="ts">
import { RouterView } from "vue-router";

import AdminHeader from "@/components/sub2api/layout/AdminHeader.vue";
import ShellLayout from "@/components/sub2api/layout/ShellLayout.vue";
import SidebarNav from "@/components/sub2api/layout/SidebarNav.vue";
import { useUiStore } from "@/stores/ui";

const ui = useUiStore();

function toggleDesktopNavigation(): void {
  ui.navigationCollapsed = !ui.navigationCollapsed;
}
</script>

<template>
  <ShellLayout :collapsed="ui.navigationCollapsed">
    <template #sidebar>
      <button v-if="ui.mobileNavigationOpen" class="fixed inset-0 z-30 bg-slate-950/45 lg:hidden" type="button" aria-label="关闭导航遮罩" @click="ui.closeMobileNavigation" />
      <SidebarNav :collapsed="ui.navigationCollapsed" :open="ui.mobileNavigationOpen" @collapse="toggleDesktopNavigation" @close="ui.closeMobileNavigation" @navigate="ui.closeMobileNavigation" />
    </template>
    <template #header><AdminHeader :mobile-navigation-open="ui.mobileNavigationOpen" @toggle-navigation="ui.toggleNavigation" /></template>
    <RouterView />
  </ShellLayout>
</template>
