<script setup lang="ts">
import { computed } from "vue";

import StatePill from "@/components/StatePill.vue";

const props = defineProps<{ operation: Record<string, unknown> | null; title?: string }>();
const action = computed(() => String(props.operation?.action ?? props.title ?? "操作"));
const status = computed(() => String(props.operation?.status ?? "pending"));
const identifier = computed(() => props.operation?.operation_id ?? props.operation?.run_id ?? null);
const error = computed(() => props.operation?.error ?? props.operation?.error_code ?? null);
const completedAt = computed(() => props.operation?.finished_at ?? props.operation?.created_at ?? null);
</script>

<template>
  <article v-if="operation" class="operation-status" :class="`operation-status--${status}`" role="status">
    <div><strong>{{ action }}</strong><span v-if="identifier" class="mono">{{ identifier }}</span></div>
    <StatePill :value="status" />
    <p v-if="error" class="text-danger">{{ error }}</p>
    <time v-if="completedAt">{{ completedAt }}</time>
  </article>
</template>
