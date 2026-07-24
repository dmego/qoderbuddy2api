<!-- SPDX-License-Identifier: LGPL-3.0-or-later
  Derived from Wei-Shaw/sub2api frontend/src/components/layout/AppLayout.vue at cb24522.
-->
<script setup lang="ts">
defineProps<{ collapsed: boolean }>();
</script>

<template>
  <div class="min-h-screen bg-gray-50 dark:bg-dark-950">
    <div class="pointer-events-none fixed inset-0 bg-mesh-gradient" aria-hidden="true" />
    <slot name="sidebar" />
    <div class="relative min-h-screen transition-[margin] duration-300" :class="collapsed ? 'lg:ml-[72px]' : 'lg:ml-64'">
      <slot name="header" />
      <main class="p-4 md:p-6 lg:p-8"><slot /></main>
    </div>
  </div>
</template>
