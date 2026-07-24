<script setup lang="ts">
import { computed } from "vue";

import StatePill from "@/components/StatePill.vue";

const props = defineProps<{ operation: Record<string, unknown> | null; title?: string }>();
const action = computed(() => String(props.operation?.action ?? props.title ?? "操作"));
const status = computed(() => String(props.operation?.status ?? "pending"));
const identifier = computed(() => props.operation?.operation_id ?? props.operation?.run_id ?? null);
const error = computed(() => props.operation?.error ?? props.operation?.error_code ?? null);
const completedAt = computed(() => props.operation?.finished_at ?? props.operation?.created_at ?? null);
const items = computed(() => Array.isArray(props.operation?.items) ? props.operation.items as Record<string, unknown>[] : []);

function itemLabel(item: Record<string, unknown>): string {
  const identity = [item.provider, item.account_id].filter(Boolean).join(" / ");
  return String(item.label ?? item.key ?? (identity || "明细"));
}
function itemError(item: Record<string, unknown>): string {
  return String(item.error ?? item.error_code ?? "");
}
</script>

<template>
  <article v-if="operation" class="operation-status" :class="`operation-status--${status}`" role="status">
    <div><strong>{{ action }}</strong><span v-if="identifier" class="mono">{{ identifier }}</span></div>
    <StatePill :value="status" />
    <p v-if="error" class="text-danger">{{ error }}</p>
    <time v-if="completedAt">{{ completedAt }}</time>
    <ul v-if="items.length" class="operation-items">
      <li v-for="item in items" :key="String(item.key ?? `${item.provider}:${item.account_id}`)">
        <StatePill :value="String(item.status ?? 'pending')" /><span>{{ itemLabel(item) }}</span><small v-if="itemError(item)" class="text-danger">{{ itemError(item) }}</small>
      </li>
    </ul>
  </article>
</template>
