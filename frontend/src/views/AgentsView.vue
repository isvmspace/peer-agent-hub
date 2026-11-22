<script setup>
import { computed, h, onMounted, ref } from 'vue'
import { NButton, NDropdown, NSpace, NTag, useDialog, useMessage } from 'naive-ui'
import { Ellipsis, RefreshCw, Search } from '@lucide/vue'
import PageHeader from '../components/PageHeader.vue'
import ResponsiveTable from '../components/ResponsiveTable.vue'
import NewTaskModal from '../components/NewTaskModal.vue'
import LocalSessionsDialog from '../components/LocalSessionsDialog.vue'
import { api } from '../api/client'
import { useRouter } from 'vue-router'
import { useAuth } from '../composables/useAuth'
import { ISOLATION, formatTime } from '../utils/format'
import { t } from '../i18n'

const message = useMessage()
const dialog = useDialog()
const { state: auth } = useAuth()
const router = useRouter()
const agents = ref([])
const loading = ref(false)
const keyword = ref('')
const scope = ref('all')
const target = ref('')
const showNew = ref(false)
const wsAgent = ref(null)
const wsRows = ref([])
const wsBusy = ref(false)
const sessionsAgent = ref(null)

function editWorkspaces(row) {
  wsAgent.value = row
  wsRows.value = (row.workspaces || []).map(w => ({ ...w }))
  if (!wsRows.value.length) wsRows.value.push({ name: '', path: '' })
}

async function saveWorkspaces() {
  wsBusy.value = true
  try {
    const result = await api(`/api/agents/${wsAgent.value.id}/workspaces`, {
      method: 'PUT',
      body: { workspaces: wsRows.value.filter(w => w.path.trim()) },
    })
    if (result.warning) message.warning(result.warning)
    else message.success(t('工作区已更新'))
    wsAgent.value = null
    load()
  } catch (e) { message.error(e.message) } finally { wsBusy.value = false }
}

const filtered = computed(() => {
  const k = keyword.value.trim().toLowerCase()
  const list = scope.value === 'mine' ? agents.value.filter(a => a.user_id === auth.user?.id) : agents.value
  if (!k) return list
  return list.filter(a =>
    [a.address, a.name, a.description, a.owner_name, a.hostname, ...(a.tags || [])].join(' ').toLowerCase().includes(k),
  )
})

// A newer load or a visibility change bumps loadSeq so a stale list response can't overwrite it.
let loadSeq = 0
async function load() {
  const seq = ++loadSeq
  loading.value = true
  try {
    const list = (await api('/api/agents')).agents
    if (seq === loadSeq) agents.value = list
  } catch (e) { if (seq === loadSeq) message.error(e.message) } finally { if (seq === loadSeq) loading.value = false }
}

function remove(row) {
  dialog.warning({
    title: t('移除智能体'),
    content: t('确认移除 {address}？连接器仍配置它时会在下次连接重新注册。', { address: row.address }),
    positiveText: t('移除'),
    negativeText: t('取消'),
    onPositiveClick: async () => {
      try { await api(`/api/agents/${row.id}`, { method: 'DELETE' }); message.success(t('已移除')); load() } catch (e) { message.error(e.message) }
    },
  })
}

// Hiding only blocks discovery and new requests; existing grants stay until revoked in chat.
async function toggleVisible(row) {
  const hidden = !row.hidden
  try {
    await api(`/api/agents/${row.id}/visibility`, { method: 'PUT', body: { hidden } })
    loadSeq++
    loading.value = false
    for (const a of agents.value) if (a.id === row.id) a.hidden = hidden ? 1 : 0
    if (hidden) message.success(t('{name} 已改为仅自己可见：同事看不到它，也无法再申请使用；已授权的同事不受影响', { name: row.name }), { duration: 6000 })
    else message.success(t('{name} 已对同事可见', { name: row.name }))
  } catch (e) { message.error(e.message) }
}

async function call(path, body, done) {
  try { await api(path, { method: 'POST', body }); message.success(done); load() } catch (e) { message.error(e.message) }
}

// Others' agents need a friendship with the owner, then a granted access request, before they can be used.
function accessActions(row) {
  const btn = (props, label) => h(NButton, { size: 'small', ...props }, () => label)
  if (row.access === 'owner' || row.access === 'accepted' || auth.user?.is_admin) {
    return [
      btn({ secondary: true, onClick: () => router.push({ path: '/chat', query: { agent: row.id } }) }, t('对话')),
      btn({ type: 'primary', secondary: true, onClick: () => { target.value = row.address; showNew.value = true } }, t('派任务')),
    ]
  }
  if (row.access === 'pending') return [btn({ disabled: true }, t('已申请，待授权'))]
  if (row.is_friend) return [btn({ type: 'primary', dashed: true, onClick: () => call('/api/contacts', { agent: row.address }, t('已申请，等待对方授权工作区')) }, t('申请使用'))]
  return [btn({ dashed: true, onClick: () => call('/api/friends', { user: row.username }, t('已发送好友申请')) }, t('加 {name} 为好友', { name: row.owner_name || row.username }))]
}

function moreActions(row) {
  const mine = row.user_id === auth.user?.id
  return [
    mine ? { key: 'visibility', label: row.hidden ? t('对同事可见') : t('改为仅自己可见') } : null,
    mine ? { key: 'workspaces', label: t('管理工作区') } : null,
    mine && ['claude', 'codex'].includes(row.kind) ? { key: 'sessions', label: t('分享本机会话'), disabled: !row.online } : null,
    mine || auth.user?.is_admin ? { key: 'remove', label: t('移除智能体') } : null,
  ].filter(Boolean)
}
function onMore(row, key) {
  if (key === 'visibility') toggleVisible(row)
  else if (key === 'workspaces') editWorkspaces(row)
  else if (key === 'sessions') sessionsAgent.value = row
  else if (key === 'remove') remove(row)
}

// what the agent is doing right now: connector telemetry, or the hub's own view for older connectors
function loadLine(row) {
  const l = row.load
  if (!row.online || !l) return null
  const busy = l.running > 0
  const text = t('运行 {running}/1 · 排队 {queued}', { running: l.running, queued: l.queued }) + (l.queued && l.oldest_wait_s ? ' · ' + t('最长等待 {s} 秒', { s: l.oldest_wait_s }) : '')
  return h('span', { class: ['load-line', { busy, queued: l.queued > 0 }], title: l.source === 'connector' ? t('连接器实时上报') : t('按 Hub 记录估算（连接器版本较旧）') }, [
    h('span', { class: 'load-line__meter', 'aria-hidden': 'true' }, [h('i', { style: { width: busy ? '100%' : '0%' } })]),
    text,
    l.source === 'hub' ? h('span', { class: 'load-line__source' }, t('（按 Hub 记录估算）')) : null,
    l.lock_wait ? h('em', null, ' · ' + t('等待工作区锁（#{id}）', { id: l.lock_wait.holder_task_id })) : null,
  ])
}

const columns = [
  {
    title: t('智能体'),
    key: 'address',
    minWidth: 220,
    render: row => h('div', { class: 'primary-cell' }, [
      h('strong', null, [h('span', { class: ['dot', row.online ? 'on' : 'off'], title: row.online ? t('在线') : t('离线') }), row.name]),
      h('small', { class: 'addr' }, row.address),
      loadLine(row),
      h(NSpace, { size: 4, style: 'margin-top:6px' }, () => [
        h(NTag, { size: 'tiny', bordered: false }, () => row.kind),
        row.capabilities?.tool_trace === 'lifecycle'
          ? h(NTag, { size: 'tiny', bordered: false, type: 'info', title: t('上报结构化工具调用，可在任务「执行流」里看到每次调用的耗时') }, () => t('工具轨迹'))
          : null,
        h(NTag, { size: 'tiny', bordered: false, type: ISOLATION[row.isolation]?.type, title: ISOLATION[row.isolation]?.tip }, () => ISOLATION[row.isolation]?.label || row.isolation),
        row.hidden && row.user_id === auth.user?.id ? h(NTag, { size: 'tiny', bordered: false, class: 'tag-action', role: 'button', tabindex: 0, title: t('点击改为同事可见'), onClick: () => toggleVisible(row), onKeydown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleVisible(row) } } }, () => t('仅自己可见')) : null,
      ]),
    ]),
  },
  {
    title: t('归属人 / 电脑'),
    key: 'owner_name',
    minWidth: 190,
    render: row => h('div', { class: 'secondary-cell' }, [
      h('strong', null, row.owner_name || row.username),
      h('small', null, `${row.hostname || '—'}，${row.os || ''}`),
      h('small', null, t('更新于 {time}', { time: formatTime(row.updated_at) })),
    ]),
  },
  {
    title: t('说明'),
    key: 'description',
    minWidth: 200,
    render: row => h('div', { class: 'secondary-cell' }, [
      h('span', { class: 'clamp-2', title: row.description }, row.description || '—'),
      row.tags?.length ? h(NSpace, { size: 4, style: 'margin-top:6px' }, () => row.tags.map(t => h(NTag, { size: 'tiny', round: true }, () => t))) : null,
    ]),
  },
  {
    title: t('工作区'),
    key: 'workspaces',
    minWidth: 180,
    render: row => h('div', { class: 'secondary-cell' }, [
      row.user_id === auth.user?.id ? h('small', { class: 'ws-path', title: row.workdir }, t('默认目录 {dir}', { dir: row.workdir || '—' })) : h('small', null, row.workspaces?.length ? t('授权给你的工作区') : t('未授权给你')),
      ...(row.workspaces || []).map(w => h('span', { class: 'ws-item', title: w.path || w.name }, w.name)),
    ]),
  },
  {
    title: t('操作'),
    key: 'actions',
    width: 230,
    fixed: 'right',
    render: row => {
      const more = moreActions(row)
      return h(NSpace, { size: 6, wrap: false }, () => [
        ...accessActions(row),
        more.length
          ? h(NDropdown, { trigger: 'click', options: more, onSelect: key => onMore(row, key) }, () =>
              h(NButton, { size: 'small', quaternary: true, 'aria-label': t('{name} 的更多操作', { name: row.name }) }, { icon: () => h(Ellipsis) }))
          : null,
      ])
    },
  },
]

onMounted(load)
</script>

<template>
  <div class="page-wrap">
    <PageHeader :title="t('智能体')" :description="t('所有通过连接器注册上来的智能体，地址格式为 用户名/智能体标识')">
      <template #actions>
        <n-button @click="load"><template #icon><RefreshCw /></template>{{ t('刷新') }}</n-button>
      </template>
    </PageHeader>
    <n-card>
      <div class="table-toolbar">
        <n-radio-group v-model:value="scope" size="small">
          <n-radio-button value="all">{{ t('全部') }}</n-radio-button>
          <n-radio-button value="mine">{{ t('只看我的') }}</n-radio-button>
        </n-radio-group>
        <span>{{ t('共 {n} 个，在线 {online} 个', { n: filtered.length, online: filtered.filter(a => a.online).length }) }}</span>
        <n-input v-model:value="keyword" class="search-input" clearable :placeholder="t('搜索名称、归属人、电脑')">
          <template #prefix><Search :size="16" /></template>
        </n-input>
      </div>
      <ResponsiveTable :scroll-x="1040" :columns="columns" :data="filtered" :loading="loading" :row-key="r => r.id" />
    </n-card>
    <NewTaskModal v-model:show="showNew" :agent="target" />
    <LocalSessionsDialog :show="!!sessionsAgent" :agent="sessionsAgent" @update:show="v => { if (!v) sessionsAgent = null }" />
    <n-modal :show="!!wsAgent" preset="card" :title="t('授权工作区 · {address}', { address: wsAgent?.address })" class="form-modal" @update:show="v => { if (!v) wsAgent = null }">
      <p class="hint">{{ t('任务只能在这些文件夹里执行（派任务时可选）。路径必须是 {host} 上已存在的绝对路径，由连接器在本机校验后保存；连接器离线时无法修改。', { host: wsAgent?.hostname }) }}</p>
      <div class="member-rows">
        <div v-for="(w, i) in wsRows" :key="i" class="member-row">
          <n-input v-model:value="w.name" :placeholder="t('名称，如 桌面客户端')" />
          <n-input v-model:value="w.path" :placeholder="t('绝对路径，如 {path}', { path: 'D:\\code\\desktop-client' })" />
          <n-button quaternary circle @click="wsRows.splice(i, 1)">✕</n-button>
        </div>
        <n-button dashed size="small" @click="wsRows.push({ name: '', path: '' })">{{ t('添加文件夹') }}</n-button>
      </div>
      <template #footer><n-space justify="end"><n-button @click="wsAgent = null">{{ t('取消') }}</n-button><n-button type="primary" :loading="wsBusy" @click="saveWorkspaces">{{ t('保存') }}</n-button></n-space></template>
    </n-modal>
  </div>
</template>
