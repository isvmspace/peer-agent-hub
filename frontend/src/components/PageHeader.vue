<script setup>
defineProps({
  title: { type: String, required: true },
  description: { type: String, default: '' },
})
</script>

<template>
  <header class="page-header">
    <div>
      <h2>{{ title }}</h2>
      <p v-if="description">{{ description }}</p>
    </div>
    <n-space>
      <slot name="actions" />
    </n-space>
  </header>
</template>
