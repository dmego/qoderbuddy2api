<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{ value: string | boolean | null | undefined }>();
const className = computed(() => `state-pill--${String(props.value ?? "unknown").toLowerCase().replaceAll("_", "-")}`);
</script>

<template>
  <span class="state-pill" :class="className">
    <span class="state-pill__dot" aria-hidden="true"></span>
    {{ typeof props.value === "boolean" ? (props.value ? "启用" : "停用") : props.value ?? "未知" }}
  </span>
</template>
