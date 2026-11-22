<script setup>
// Three linked nodes: one agent hands work to two others.
defineProps({ size: { type: Number, default: 34 } })
</script>

<template>
  <svg class="brand-mark" :width="size" :height="size" viewBox="0 0 34 34" aria-hidden="true">
    <rect x="0.5" y="0.5" width="33" height="33" rx="9" class="brand-mark__tile" />
    <path d="M11 22 L17 11 L24 21" class="brand-mark__link" />
    <path d="M11 22 L24 21" class="brand-mark__link brand-mark__link--flow" />
    <circle cx="17" cy="11" r="3.4" class="brand-mark__node brand-mark__node--hub" />
    <circle cx="11" cy="22" r="2.8" class="brand-mark__node" />
    <circle cx="24" cy="21" r="2.8" class="brand-mark__node" />
  </svg>
</template>
