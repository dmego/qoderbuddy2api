<!-- SPDX-License-Identifier: LGPL-3.0-or-later
  Derived from Wei-Shaw/sub2api frontend/src/components/common/StatusBadge.vue at cb24522.
-->
<script setup lang="ts">
import { computed } from "vue";

import { statusLabel } from "@/utils/presentation";

const props = defineProps<{ value: string | boolean | null | undefined }>();
const tone = computed(() => {
  const value = String(props.value ?? "unknown").toLowerCase();
  if (["healthy", "active", "enabled", "success", "succeeded", "completed", "verified"].includes(value)) return "success";
  if (["failed", "error", "disabled", "cancelled", "revoked"].includes(value)) return "danger";
  if (["action_required", "warning", "stale", "pending", "running", "unavailable"].includes(value)) return "warning";
  return "neutral";
});
const toneClass = computed(() => ({
  success: "bg-emerald-500", warning: "bg-amber-500", danger: "bg-red-500", neutral: "bg-gray-400 dark:bg-dark-500",
}[tone.value]));
</script>

<template>
  <span class="inline-flex items-center gap-1.5 text-sm text-gray-700 dark:text-dark-300" :data-tone="tone"><span class="size-2 rounded-full" :class="toneClass" aria-hidden="true" />{{ statusLabel(value) }}</span>
</template>
