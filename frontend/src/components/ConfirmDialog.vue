<script setup lang="ts">
import { AlertTriangle, LoaderCircle, X } from "@lucide/vue";
import { nextTick, ref, watch } from "vue";

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
const dialog = ref<HTMLElement | null>(null);
let previousFocus: HTMLElement | null = null;

watch(() => props.open, async (open) => {
  if (open) {
    previousFocus = document.activeElement as HTMLElement | null;
    confirmation.value = "";
    await nextTick();
    dialog.value?.querySelector<HTMLElement>("button, input")?.focus();
  } else {
    previousFocus?.focus();
  }
});

function cancel(): void {
  if (!props.busy) emit("cancel");
}

function trapFocus(event: KeyboardEvent): void {
  if (event.key !== "Tab" || !dialog.value) return;
  const focusable = [...dialog.value.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled)")];
  const first = focusable[0];
  const last = focusable.at(-1);
  if (!first || !last) return;
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="dialog-backdrop" @mousedown.self="cancel">
      <section ref="dialog" class="confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby="confirm-dialog-title" aria-describedby="confirm-dialog-description" @keydown.esc="cancel" @keydown="trapFocus">
        <div class="dialog-heading">
          <span class="dialog-icon" :class="{ 'dialog-icon--danger': tone === 'danger' }"><AlertTriangle :size="20" /></span>
          <div><h2 id="confirm-dialog-title">{{ title }}</h2><p id="confirm-dialog-description">{{ description }}</p></div>
          <button class="icon-button" type="button" aria-label="关闭确认对话框" :disabled="busy" @click="cancel"><X :size="16" /></button>
        </div>
        <label v-if="verificationText" class="dialog-verification">输入 <strong>{{ verificationText }}</strong> 继续<input v-model="confirmation" autocomplete="off" /></label>
        <div class="dialog-actions">
          <button class="secondary-button" type="button" :disabled="busy" @click="cancel">{{ cancelLabel }}</button>
          <button type="button" :class="{ 'danger-button': tone === 'danger' }" :disabled="busy || Boolean(verificationText && confirmation !== verificationText)" @click="emit('confirm')"><LoaderCircle v-if="busy" class="spin" :size="16" />{{ confirmLabel }}</button>
        </div>
      </section>
    </div>
  </Teleport>
</template>
