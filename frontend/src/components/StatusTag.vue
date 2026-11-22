<script setup>
import { computed } from 'vue'
import { STATUS } from '../utils/format'

const props = defineProps({ status: { type: String, required: true } })
const meta = computed(() => STATUS[props.status] || { label: props.status, type: 'default' })
</script>

<template>
  <n-tag :type="meta.type" size="small" round :bordered="false">{{ meta.label }}</n-tag>
</template>
