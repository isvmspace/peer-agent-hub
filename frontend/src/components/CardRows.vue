<script setup>
// Phone layout for a naive-ui column list: the first column heads each card,
// the rest become labelled lines, and the "actions" column sits at the bottom.
import { h } from 'vue'
import { t } from '../i18n'

const props = defineProps({
  columns: { type: Array, required: true },
  data: { type: Array, default: () => [] },
  rowKey: { type: Function, required: true },
  loading: { type: Boolean, default: false },
  onRowClick: { type: Function, default: null },
})

const cell = (col, row) => (col.render ? col.render(row) : String(row[col.key] ?? '—'))
const Cell = (p) => cell(p.col, p.row)
const [head, ...rest] = props.columns
const body = rest.filter(c => c.key !== 'actions')
const actions = props.columns.find(c => c.key === 'actions')
</script>

<template>
  <n-spin :show="loading">
    <n-empty v-if="!data.length && !loading" :description="t('暂无数据')" class="card-rows__empty" />
    <ul class="card-rows">
      <li v-for="row in data" :key="rowKey(row)" class="card-row">
        <component
          :is="onRowClick ? 'button' : 'div'"
          :type="onRowClick ? 'button' : undefined"
          class="card-row__main"
          @click="onRowClick && onRowClick(row)"
        >
          <div class="card-row__head"><Cell :col="head" :row="row" /></div>
          <dl class="card-row__fields">
            <div v-for="col in body" :key="col.key">
              <dt>{{ col.title }}</dt>
              <dd><Cell :col="col" :row="row" /></dd>
            </div>
          </dl>
        </component>
        <div v-if="actions" class="card-row__actions"><Cell :col="actions" :row="row" /></div>
      </li>
    </ul>
  </n-spin>
</template>
