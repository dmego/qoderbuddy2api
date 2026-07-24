<!-- SPDX-License-Identifier: LGPL-3.0-or-later
  Derived from Wei-Shaw/sub2api frontend/src/components/layout/TablePageLayout.vue at cb24522.
-->
<template>
  <div data-testid="table-page-layout" class="flex min-h-0 flex-col gap-6 lg:h-[calc(100vh-8rem)]">
    <div v-if="$slots.actions" class="shrink-0"><slot name="actions" /></div>
    <div v-if="$slots.filters" class="shrink-0"><slot name="filters" /></div>
    <div class="min-h-0 flex-1"><slot name="table" /></div>
    <div v-if="$slots.pagination" class="shrink-0"><slot name="pagination" /></div>
  </div>
</template>
