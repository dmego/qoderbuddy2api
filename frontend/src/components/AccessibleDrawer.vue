<script setup lang="ts">
import { X } from "@lucide/vue";
import { nextTick, onBeforeUnmount, ref, useId, watch } from "vue";

const props = withDefaults(defineProps<{
  open: boolean;
  title: string;
  subtitle?: string;
  closeLabel?: string;
}>(), {
  subtitle: "",
  closeLabel: "关闭详情",
});
const emit = defineEmits<{ close: [] }>();
const panel = ref<HTMLElement | null>(null);
const titleId = `drawer-title-${useId()}`;
let previousFocus: HTMLElement | null = null;

watch(() => props.open, async (open) => {
  if (open) {
    previousFocus = document.activeElement as HTMLElement | null;
    await nextTick();
    focusableElements()[0]?.focus();
  } else restoreFocus();
}, { immediate: true });
onBeforeUnmount(restoreFocus);

function close(): void {
  emit("close");
}

function restoreFocus(): void {
  if (previousFocus?.isConnected) previousFocus.focus();
  previousFocus = null;
}

function trapFocus(event: KeyboardEvent): void {
  if (event.key !== "Tab") return;
  const focusable = focusableElements();
  const first = focusable[0];
  const last = focusable.at(-1);
  if (!first || !last) return;
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}

function focusableElements(): HTMLElement[] {
  if (!panel.value) return [];
  return [...panel.value.querySelectorAll<HTMLElement>('a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])')];
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="drawer-backdrop" @mousedown.self="close">
      <section ref="panel" class="detail-drawer data-panel" role="dialog" aria-modal="true" :aria-labelledby="titleId" @keydown.esc="close" @keydown="trapFocus">
        <div class="drawer-heading">
          <div><h2 :id="titleId">{{ title }}</h2><p v-if="subtitle" class="mono">{{ subtitle }}</p></div>
          <button class="icon-button drawer-close" type="button" :aria-label="closeLabel" @click="close"><X :size="16" /></button>
        </div>
        <slot />
      </section>
    </div>
  </Teleport>
</template>
