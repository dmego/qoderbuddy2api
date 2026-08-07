<script setup lang="ts">
import { AlertCircle, CheckCircle2, Info, TriangleAlert, X } from "@lucide/vue";

import type { ConsoleNotification } from "@/composables/useNotifications";

defineProps<{ notifications: ConsoleNotification[] }>();
defineEmits<{ dismiss: [id: number] }>();
</script>

<template>
  <div class="notification-region" aria-live="polite" aria-relevant="additions removals">
    <article v-for="item in notifications" :key="item.id" class="notification" :class="`notification--${item.tone}`" :role="item.tone === 'error' ? 'alert' : 'status'">
      <CheckCircle2 v-if="item.tone === 'success'" :size="18" />
      <AlertCircle v-else-if="item.tone === 'error'" :size="18" />
      <TriangleAlert v-else-if="item.tone === 'warning'" :size="18" />
      <Info v-else :size="18" />
      <div><strong>{{ item.title }}</strong><span v-if="item.message">{{ item.message }}</span></div>
      <button type="button" aria-label="关闭通知" @click="$emit('dismiss', item.id)"><X :size="15" /></button>
    </article>
  </div>
</template>
