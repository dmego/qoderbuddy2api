<!-- SPDX-License-Identifier: LGPL-3.0-or-later
  Derived from Wei-Shaw/sub2api frontend/src/components/common/BaseDialog.vue at cb24522.
-->
<script setup lang="ts">
import { X } from "@lucide/vue";
import { nextTick, onBeforeUnmount, ref, useId, watch } from "vue";

const props = withDefaults(defineProps<{ open: boolean; title: string; closeLabel?: string; closeOnBackdrop?: boolean }>(), {
  closeLabel: "关闭对话框",
  closeOnBackdrop: true,
});
const emit = defineEmits<{ close: [] }>();
const panel = ref<HTMLElement | null>(null);
const titleId = `sub2api-dialog-${useId()}`;
let previousFocus: HTMLElement | null = null;

function focusable(): HTMLElement[] {
  return panel.value ? [...panel.value.querySelectorAll<HTMLElement>('a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])')] : [];
}

function restoreFocus(): void {
  if (previousFocus?.isConnected) previousFocus.focus();
  previousFocus = null;
}

function close(): void { emit("close"); }

function trapFocus(event: KeyboardEvent): void {
  if (event.key !== "Tab") return;
  const items = focusable();
  const first = items[0];
  const last = items.at(-1);
  if (!first || !last) return;
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}

watch(() => props.open, async (open) => {
  if (!open) { restoreFocus(); return; }
  previousFocus = document.activeElement as HTMLElement | null;
  await nextTick();
  focusable()[0]?.focus();
}, { immediate: true });
onBeforeUnmount(restoreFocus);
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/50 p-4" @mousedown.self="closeOnBackdrop && close()">
      <section ref="panel" class="w-full max-w-lg rounded-2xl border border-gray-200 bg-white shadow-glass dark:border-dark-700 dark:bg-dark-800" role="dialog" aria-modal="true" :aria-labelledby="titleId" @keydown.esc="close" @keydown="trapFocus">
        <header class="flex items-center justify-between gap-4 border-b border-gray-200 px-5 py-4 dark:border-dark-700"><h2 :id="titleId" class="text-base font-semibold text-gray-900 dark:text-white">{{ title }}</h2><button class="inline-flex size-11 items-center justify-center rounded-xl text-gray-500 hover:bg-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500 dark:text-dark-300 dark:hover:bg-dark-700" type="button" :aria-label="closeLabel" @click="close"><X :size="18" aria-hidden="true" /></button></header>
        <div class="px-5 py-4"><slot /></div>
        <footer v-if="$slots.footer" class="border-t border-gray-200 px-5 py-4 dark:border-dark-700"><slot name="footer" /></footer>
      </section>
    </div>
  </Teleport>
</template>
