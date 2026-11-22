<script setup>
// A data table on wide screens and a card list on phones, from the same column list.
import CardRows from './CardRows.vue'
import { usePhone } from '../composables/usePhone'

defineOptions({ inheritAttrs: false })
defineProps({
  columns: { type: Array, required: true },
  data: { type: Array, default: () => [] },
  rowKey: { type: Function, required: true },
  loading: { type: Boolean, default: false },
  onRowClick: { type: Function, default: null },
})
const phone = usePhone()
</script>

<template>
  <CardRows v-if="phone" :columns="columns" :data="data" :row-key="rowKey" :loading="loading" :on-row-click="onRowClick" />
  <n-data-table
    v-else
    v-bind="$attrs"
    :columns="columns"
    :data="data"
    :row-key="rowKey"
    :loading="loading"
    :row-props="onRowClick ? row => ({ style: 'cursor:pointer', onClick: () => onRowClick(row) }) : undefined"
  />
</template>
