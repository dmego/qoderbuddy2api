<script setup lang="ts">
import { computed } from "vue";

import { statusLabel } from "@/utils/presentation";

const props = defineProps<{ value: string | boolean | null | undefined }>();
const className = computed(() => `state-pill--${String(props.value ?? "unknown").toLowerCase().replaceAll("_", "-")}`);
</script>

<template>
  <span class="state-pill" :class="className">
    <span class="state-pill__dot" aria-hidden="true"></span>
    {{ statusLabel(props.value) }}
  </span>
</template>
