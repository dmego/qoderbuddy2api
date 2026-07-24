<!-- SPDX-License-Identifier: LGPL-3.0-or-later
  Derived from Wei-Shaw/sub2api frontend/src/components/common/ConfirmDialog.vue at cb24522.
-->
<script setup lang="ts">
import { computed, ref, watch } from "vue";

import BaseDialog from "./BaseDialog.vue";

const props = withDefaults(defineProps<{
  open: boolean;
  title: string;
  description: string;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "default" | "danger";
  busy?: boolean;
  verificationText?: string;
}>(), {
  confirmLabel: "确认",
  cancelLabel: "取消",
  tone: "default",
  busy: false,
  verificationText: "",
});
const emit = defineEmits<{ confirm: []; cancel: [] }>();
const confirmation = ref("");
const confirmClass = computed(() => props.tone === "danger" ? "btn-danger" : "btn-primary");
const requiresVerification = computed(() => Boolean(props.verificationText));
const canConfirm = computed(() => !props.busy && (!requiresVerification.value || confirmation.value === props.verificationText));

function cancel(): void { if (!props.busy) emit("cancel"); }

watch(() => props.open, (isOpen) => {
  if (isOpen) confirmation.value = "";
});
</script>

<template>
  <BaseDialog :open="open" :title="title" @close="cancel">
    <p class="text-sm leading-6 text-gray-600 dark:text-dark-300">{{ description }}</p>
    <label v-if="requiresVerification" class="mt-4 block text-sm font-medium text-gray-700 dark:text-dark-200">
      输入“{{ verificationText }}”以继续
      <input v-model="confirmation" class="input mt-2" aria-label="确认文本" autocomplete="off" />
    </label>
    <slot />
    <template #footer>
      <div class="flex justify-end gap-3">
        <button class="btn-secondary" type="button" :disabled="busy" @click="cancel">{{ cancelLabel }}</button>
        <button :class="confirmClass" type="button" data-action="confirm" :disabled="!canConfirm" @click="emit('confirm')">
          {{ confirmLabel }}
        </button>
      </div>
    </template>
  </BaseDialog>
</template>
