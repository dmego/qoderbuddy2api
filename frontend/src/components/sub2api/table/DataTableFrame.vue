<!-- SPDX-License-Identifier: LGPL-3.0-or-later
  Derived from Wei-Shaw/sub2api frontend/src/components/common/DataTable.vue at cb24522.
-->
<script setup lang="ts">
withDefaults(defineProps<{
  label: string;
  loading?: boolean;
}>(), { loading: false });
</script>

<template>
  <div class="flex h-full min-h-0 flex-col overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-card dark:border-dark-700 dark:bg-dark-800">
    <div class="min-h-0 flex-1 overflow-auto" :aria-busy="loading">
      <table class="w-full min-w-max border-collapse text-left text-sm">
        <caption class="sr-only">{{ label }}</caption>
        <thead class="sticky top-0 z-10 bg-gray-50/90 backdrop-blur dark:bg-dark-800/90"><slot name="header" /></thead>
        <tbody><slot /></tbody>
      </table>
    </div>
  </div>
</template>
