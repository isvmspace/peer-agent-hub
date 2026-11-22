<script setup>
import { computed, h, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NSpace, NTag, useMessage } from 'naive-ui'
import { Send } from '@lucide/vue'
import PageHeader from '../components/PageHeader.vue'
import ResponsiveTable from '../components/ResponsiveTable.vue'
import { usePhone } from '../composables/usePhone'
import StatusTag from '../components/StatusTag.vue'
import NewTaskModal from '../components/NewTaskModal.vue'
import { api } from '../api/client'
import { useAuth } from '../composables/useAuth'
import { STATUS, duration, formatTime } from '../utils/format'
import { t } from '../i18n'

const emit = defineEmits(['changed'])
const route = useRoute()
const router = useRouter()
const message = useMessage()
const { state: auth } = useAuth()
const BOXES = ['all', 'sent', 'received', 'approval', 'shared', 'everyone']
const LIMIT = 200
// the URL is the source of truth, so links like the header's approval pill work on this page too
const box = computed({
  get: () => (BOXES.includes(route.query.box) ? route.query.box : 'all'),
  set: value => router.push({ query: { ...route.query, box: value, status: value === 'approval' ? undefined : route.query.status } }),
})
const status = computed({
  get: () => (box.value !== 'approval' && STATUS[route.query.status] ? route.query.status : null),
  set: value => router.push({ query: { ...route.query, status: value || undefined } }),
})
const tasks = ref([])
const loading = ref(false)
const showNew = ref(false)
const phone = usePhone()
const boxOptions = computed(() => [
  { value: 'all', label: t('全部') }, { value: 'sent', label: t('我发出的') }, { value: 'received', label: t('派给我的') },
  { value: 'approval', label: t('待我审批') }, { value: 'shared', label: t('分享给我') },
  ...(auth.user?.is_admin ? [{ value: 'everyone', label: t('全站') }] : []),
])
const openTask = row => router.push({ name: 'task-detail', params: { id: row.id } })
const statusOptions = Object.entries(STATUS).map(([value, m]) => ({ value, label: m.label }))

let seq = 0
async function load(silent = false) {
  const mine = ++seq // a slower, older request must not overwrite the current filter's rows
  if (!silent) loading.value = true
  try {
    const q = new URLSearchParams({ box: box.value, status: status.value || '', limit: String(LIMIT) })
    const rows = (await api(`/api/tasks?${q}`)).tasks
    if (mine === seq) tasks.value = rows
  } catch (e) {
    if (!silent && mine === seq) message.error(e.message)
  } finally {
    if (mine === seq) loading.value = false
  }
}

async function act(row, action) {
  try {
    await api(`/api/tasks/${row.id}/${action}`, { method: 'POST' })
    message.success({ approve: t('已批准'), reject: t('已拒绝'), cancel: t('已取消') }[action])
    emit('changed')
    load(true)
  } catch (e) {
    message.error(e.message)
  }
}

const columns = [
  { title: 'ID', key: 'id', width: 70, render: row => `#${row.id}` },
  {
    title: t('任务'),
    key: 'title',
    render: row => h('div', { class: 'primary-cell' }, [
      h('strong', null, row.title),
      h('small', null, `${row.from_username}${row.from_agent ? '/' + row.from_agent : ''} → ${row.agent_address}` + (row.turn > 1 ? ' · ' + t('第 {n} 轮', { n: row.turn }) : '') + (row.parent_task_id ? ' · ' + t('#{id} 的子任务', { id: row.parent_task_id }) : '')),
    ]),
  },
  {
    title: t('状态'),
    key: 'status',
    width: 130,
    render: row => h(NSpace, { size: 4 }, () => [h(StatusTag, { status: row.status }), row.auto_approved ? h(NTag, { size: 'tiny', bordered: false }, () => t('免审')) : null]),
  },
  { title: t('创建时间'), key: 'created_at', width: 140, render: row => formatTime(row.created_at) },
  { title: t('耗时'), key: 'dur', width: 110, render: row => duration(row.created_at, row.finished_at) },
  {
    title: t('操作'),
    key: 'actions',
    width: 200,
    render: row => h(NSpace, { size: 6 }, () => [
      h(NButton, { size: 'small', secondary: true, onClick: e => { e.stopPropagation(); router.push({ name: 'task-detail', params: { id: row.id } }) } }, () => t('详情')),
      box.value === 'approval' || row.status === 'pending_approval'
        ? [
            box.value === 'approval' ? h(NButton, { size: 'small', type: 'primary', onClick: e => { e.stopPropagation(); act(row, 'approve') } }, () => t('批准')) : null,
            box.value === 'approval' ? h(NButton, { size: 'small', onClick: e => { e.stopPropagation(); act(row, 'reject') } }, () => t('拒绝')) : null,
          ]
        : null,
    ]),
  },
]

watch([box, status], () => load())

let timer = null
onMounted(() => { load(); timer = setInterval(() => load(true), 5000) })
onBeforeUnmount(() => clearInterval(timer))
</script>

<template>
  <div class="page-wrap">
    <PageHeader :title="t('任务中心')" :description="t('我发出的、派给我智能体的任务，以及等待我审批的任务')">
      <template #actions>
        <n-button type="primary" @click="showNew = true"><template #icon><Send /></template>{{ t('派发任务') }}</n-button>
      </template>
    </PageHeader>
    <n-card>
      <div class="table-toolbar">
        <n-select v-if="phone" v-model:value="box" :options="boxOptions" size="small" class="domain-filter" :aria-label="t('任务范围')" />
        <n-radio-group v-else v-model:value="box" size="small">
          <n-radio-button value="all">{{ t('全部') }}</n-radio-button>
          <n-radio-button value="sent">{{ t('我发出的') }}</n-radio-button>
          <n-radio-button value="received">{{ t('派给我的') }}</n-radio-button>
          <n-radio-button value="approval">{{ t('待我审批') }}</n-radio-button>
          <n-radio-button value="shared">{{ t('分享给我') }}</n-radio-button>
          <n-radio-button v-if="auth.user?.is_admin" value="everyone">{{ t('全站') }}</n-radio-button>
        </n-radio-group>
        <n-select v-model:value="status" class="domain-filter" clearable :placeholder="t('按状态筛选')" :options="statusOptions" size="small" />
      </div>
      <ResponsiveTable :columns="columns" :data="tasks" :loading="loading" :row-key="r => r.id" :on-row-click="openTask" />
      <p v-if="tasks.length >= LIMIT" class="hint list-cap">{{ t('只显示最近 {n} 条，更早的任务请按状态筛选缩小范围', { n: LIMIT }) }}</p>
    </n-card>
    <NewTaskModal v-model:show="showNew" />
  </div>
</template>
