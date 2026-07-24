<script setup lang="ts">
defineProps<{ title: string; description?: string }>();
</script>

<template>
  <header class="panel-heading">
    <div>
      <h2>{{ title }}</h2>
      <p v-if="description">{{ description }}</p>
    </div>
    <div class="panel-actions"><slot /></div>
  </header>
</template>
