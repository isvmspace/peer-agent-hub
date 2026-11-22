<script setup>
// The delegation tree around a task on one shared time axis. Each row is a task:
// its bar runs from creation to finish (or now), tool calls are drawn inside it,
// and an unfinished task says what it is waiting for. Data refreshes every 2 s
// while anything in the tree is still active.
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useDialog, useMessage } from 'naive-ui'
import { Ban, CircleDashed, Lock, Radio, Wrench } from '@lucide/vue'
import StatusTag from './StatusTag.vue'
import { api } from '../api/client'
import { ACTIVE_STATUSES, duration, formatTime } from '../utils/format'
import { t } from '../i18n'

const props = defineProps({ taskId: { type: Number, required: true }, canCancel: { type: Boolean, default: false } })
const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const tree = ref(null)
const error = ref('')
const selected = ref(null)
const now = ref(Date.now() / 1000)
let seq = 0
let timer = null
let disposed = false

// a canceled task whose process is not yet confirmed stopped is still live
const isLive = n => ACTIVE_STATUSES.includes(n.status) || n.stop_pending
const live = computed(() => tree.value?.nodes.some(isLive))
const stopping = computed(() => tree.value?.nodes.filter(n => n.stop_pending).length || 0)

async function load() {
  const mine = ++seq
  try {
    const result = await api(`/api/tasks/${props.taskId}/tree`)
    if (disposed || mine !== seq) return
    tree.value = result
    error.value = ''
    if (!selected.value || !result.nodes.some(n => n.id === selected.value)) selected.value = result.focus_id
  } catch (e) {
    if (!disposed && mine === seq) error.value = e.message
  }
  now.value = Date.now() / 1000
}

function schedule() {
  clearTimeout(timer)
  if (disposed) return
  timer = setTimeout(async () => { await load(); if (live.value && !disposed) schedule() }, 2000)
}
watch(() => props.taskId, async () => { tree.value = null; selected.value = null; await load(); schedule() }, { immediate: true })
watch(live, on => { if (on) schedule() })
onBeforeUnmount(() => { disposed = true; seq++; clearTimeout(timer) })

const rows = computed(() => {
  const nodes = tree.value?.nodes || []
  const byParent = new Map()
  for (const n of nodes) {
    const key = n.parent_task_id ?? 0
    if (!byParent.has(key)) byParent.set(key, [])
    byParent.get(key).push(n)
  }
  const out = []
  const walk = (node, level, last, guides) => {
    out.push({ ...node, level, last, guides })
    const kids = byParent.get(node.id) || []
    kids.forEach((k, i) => walk(k, level + 1, i === kids.length - 1, [...guides, !last]))
  }
  const root = nodes.find(n => n.id === tree.value?.root_id)
  if (root) walk(root, 0, true, [])
  return out
})

const axis = computed(() => {
  const nodes = tree.value?.nodes || []
  if (!nodes.length) return { start: 0, end: 1 }
  const start = Math.min(...nodes.map(n => n.created_at))
  // only work still in progress reaches up to now; a finished tree ends where its last task ended
  const end = Math.max(...nodes.map(n => (isLive(n) ? now.value : n.finished_at || n.updated_at)))
  return { start, end: Math.max(end, start + 1) }
})
const pct = t => `${(((t - axis.value.start) / (axis.value.end - axis.value.start)) * 100).toFixed(2)}%`
const width = (a, b) => `${Math.max(0.4, ((b - a) / (axis.value.end - axis.value.start)) * 100).toFixed(2)}%`

const ticks = computed(() => {
  const span = axis.value.end - axis.value.start
  return [0, 0.25, 0.5, 0.75, 1].map(f => ({ left: `${f * 100}%`, label: f === 0 ? formatTime(axis.value.start).slice(-8) : '+' + duration(axis.value.start, axis.value.start + span * f) }))
})

function toolSpans(node) {
  return (node.timeline || []).filter(s => s.type === 'tool').map(s => {
    const end = s.end ?? (isLive(node) ? now.value : node.finished_at || node.updated_at)
    return { ...s, left: pct(s.start ?? end), width: s.start ? width(s.start, end) : '0', point: !s.start }
  })
}

const current = computed(() => tree.value?.nodes.find(n => n.id === selected.value))
const calls = computed(() => (current.value?.timeline || []).filter(s => s.type === 'tool').slice().reverse())
const usage = computed(() => (current.value?.timeline || []).filter(s => s.type === 'usage'))
const tokenTotal = computed(() => usage.value.reduce((acc, u) => ({
  input: acc.input + (u.usage.input_tokens || 0), output: acc.output + (u.usage.output_tokens || 0),
  cost: acc.cost + (u.usage.cost_usd || 0), costKnown: acc.costKnown || 'cost_usd' in u.usage,
}), { input: 0, output: 0, cost: 0, costKnown: false }))

function callDuration(s) {
  if (!s.start) return t('只观察到结束')
  if (!s.end) return t('进行中 {dur}', { dur: duration(s.start) })
  return duration(s.start, s.end)
}

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text)
    message.success(t('已复制'))
  } catch {
    message.warning(t('浏览器不允许复制，请手动选择文字'))
  }
}

function cancelTree() {
  dialog.warning({
    title: t('取消整棵任务树'),
    content: t('会取消这个任务及其所有进行中的子任务。正在运行的进程会被结束，连接器确认停止前显示“等待确认停止”。'),
    positiveText: t('全部取消'),
    negativeText: t('不取消'),
    onPositiveClick: async () => {
      try {
        await api(`/api/tasks/${tree.value.root_id}/cancel`, { method: 'POST', body: { tree: true } })
        message.success(t('已发出取消，等待连接器确认进程停止'))
        load()
      } catch (e) { message.error(e.message) }
    },
  })
}
</script>

<template>
  <div class="flow">
    <div class="flow-head">
      <span class="flow-live" :class="{ on: live }" role="status" aria-live="polite"><Radio :size="14" />{{ stopping ? t('{n} 个任务等待确认进程已停止', { n: stopping }) : live ? t('实时更新中') : t('已全部结束') }}</span>
      <small v-if="tree">{{ t('{n} 个任务 · 用时 {dur}', { n: tree.nodes.length, dur: duration(axis.start, axis.end) }) }}</small>
      <n-button v-if="canCancel && live" size="small" type="error" secondary @click="cancelTree"><template #icon><Ban /></template>{{ t('取消整棵任务树') }}</n-button>
    </div>
    <n-alert v-if="error" type="warning" :bordered="false">{{ error }}</n-alert>

    <div v-if="tree" class="flow-grid" role="list" :aria-label="t('任务树时间线：选中一行查看详情，再按 Enter 或双击打开该任务')">
      <div class="flow-axis" aria-hidden="true">
        <span class="flow-axis__label">{{ t('任务') }}</span>
        <div class="flow-axis__track"><span v-for="tk in ticks" :key="tk.left" :style="{ left: tk.left }">{{ tk.label }}</span></div>
      </div>
      <div v-for="n in rows" :key="n.id" role="listitem" class="flow-item">
      <button
        type="button"
        :aria-pressed="n.id === selected"
        :class="['flow-row', 'status-' + n.status, { selected: n.id === selected, focus: n.id === tree.focus_id }]"
        :aria-label="n.visible ? t('任务 #{id} {title}，{agent}，{state}', { id: n.id, title: n.title, agent: n.agent_address, state: n.wait?.text || n.status }) : t('任务 #{id} 无权查看，状态 {state}', { id: n.id, state: n.status })"
        @click="selected = n.id"
        @dblclick="n.visible && router.push({ name: 'task-detail', params: { id: n.id } })"
        @keydown.enter.prevent="n.id === selected && n.visible ? router.push({ name: 'task-detail', params: { id: n.id } }) : (selected = n.id)"
      >
        <span class="flow-row__name">
          <span v-for="(g, i) in n.guides.slice(1)" :key="i" class="tree-guide" :class="{ line: g }" />
          <span v-if="n.level" class="tree-elbow" :class="{ last: n.last }" />
          <span class="flow-dot" />
          <span class="flow-row__text">
            <strong>#{{ n.id }} {{ n.visible ? n.title : t('无权查看的任务') }}</strong>
            <small v-if="n.visible">{{ n.from_agent ? n.from_username + '/' + n.from_agent + ' → ' : '' }}{{ n.agent_address }}</small>
            <small v-if="n.stop_pending" class="flow-wait wait-stopping"><CircleDashed :size="12" />{{ t('已取消，等待确认进程已停止') }}</small>
            <small v-else-if="n.wait?.text" class="flow-wait" :class="'wait-' + n.wait.code">
              <Lock v-if="n.wait.code === 'workspace_lock'" :size="12" /><Wrench v-else-if="n.wait.code === 'tool'" :size="12" /><CircleDashed v-else :size="12" />{{ n.wait.text }}
            </small>
          </span>
        </span>
        <span class="flow-row__track">
          <span class="flow-bar" :style="{ left: pct(n.created_at), width: width(n.created_at, isLive(n) && !n.finished_at ? now : n.finished_at || n.updated_at) }" />
          <span
            v-for="(s, i) in toolSpans(n)"
            :key="i"
            :class="['flow-tool', 'phase-' + s.phase, { point: s.point }]"
            :style="{ left: s.left, width: s.width }"
            :title="`${s.name} ${s.summary || ''}（${callDuration(s)}）`"
          />
          <n-tag v-if="n.attempts > n.turn" size="tiny" :bordered="false" type="warning" class="flow-badge" :title="t('共下发 {a} 次 / {b} 轮', { a: n.attempts, b: n.turn })">{{ t('重发 {n} 次', { n: n.attempts - n.turn }) }}</n-tag>
          <n-tag v-if="n.stop_pending" size="tiny" :bordered="false" type="error" class="flow-badge">{{ t('等待确认停止') }}</n-tag>
        </span>
      </button>
      </div>
    </div>
    <n-skeleton v-else-if="!error" text :repeat="4" />

    <section v-if="current" class="flow-detail">
      <header>
        <strong>#{{ current.id }} {{ current.visible ? current.title : t('无权查看的任务') }}</strong>
        <StatusTag :status="current.status" />
        <n-button v-if="current.visible && current.id !== taskId" text type="primary" @click="router.push({ name: 'task-detail', params: { id: current.id } })">{{ t('打开') }}</n-button>
      </header>
      <p v-if="!current.visible" class="hint">{{ t('这个子任务由他人的智能体执行，你只能看到它的状态和时间。') }}</p>
      <template v-else>
        <p v-if="current.activity" class="flow-activity"><span>{{ t('最新活动') }}</span>{{ current.activity.summary || '—' }} <small>{{ formatTime(current.activity.at) }}</small></p>
        <p v-if="usage.length" class="flow-usage">
          <span>{{ t('用量（CLI 报告）') }}</span>{{ t('输入 {i} / 输出 {o} tokens', { i: tokenTotal.input.toLocaleString(), o: tokenTotal.output.toLocaleString() }) }}
          <template v-if="tokenTotal.costKnown">{{ t('，约 ${cost}', { cost: tokenTotal.cost.toFixed(4) }) }}</template>
        </p>
        <p v-else class="flow-usage muted">{{ t('没有用量数据（该智能体不上报或尚未结束）') }}</p>
        <ol v-if="calls.length" class="flow-calls">
          <li v-for="(s, i) in calls" :key="i" :class="'phase-' + s.phase">
            <details>
              <summary>
                <span class="flow-calls__name">{{ s.name }}</span>
                <span class="flow-calls__summary">{{ s.summary || s.result || '' }}</span>
                <small>{{ callDuration(s) }}<template v-if="s.exit_code !== undefined"> · exit {{ s.exit_code }}</template></small>
              </summary>
              <dl class="flow-calls__detail">
                <div><dt>{{ t('调用') }}</dt><dd>{{ s.summary || '—' }}</dd></div>
                <div v-if="s.result && s.result !== s.summary"><dt>{{ t('结果') }}</dt><dd>{{ s.result }}</dd></div>
                <div><dt>{{ t('开始') }}</dt><dd>{{ s.start ? formatTime(s.start) : t('未观察到（只收到结束）') }}</dd></div>
                <div><dt>{{ t('结束') }}</dt><dd>{{ s.end ? formatTime(s.end) : t('进行中') }}</dd></div>
                <div v-if="s.exit_code !== undefined"><dt>{{ t('退出码') }}</dt><dd>{{ s.exit_code }}</dd></div>
              </dl>
              <n-button size="tiny" quaternary @click="copy([s.name, s.summary, s.result].filter(Boolean).join('\n'))">{{ t('复制') }}</n-button>
            </details>
          </li>
        </ol>
        <p v-else class="hint">{{ t('没有结构化工具记录。较早的连接器和 Hermes 只上报文字过程，可在「对话」里查看。') }}</p>
      </template>
    </section>
  </div>
</template>
