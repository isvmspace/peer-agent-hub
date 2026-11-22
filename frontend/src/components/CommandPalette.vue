<script setup>
// ⌘K / Ctrl+K: one keyboard entry point to jump to a page, an agent or a task, or
// to dispatch to an agent. Results come from the same APIs the pages use.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Bot, CornerDownLeft, ListTodo, Search, Send, Workflow, Compass } from '@lucide/vue'
import NewTaskModal from './NewTaskModal.vue'
import StatusTag from './StatusTag.vue'
import { api } from '../api/client'
import { useAuth } from '../composables/useAuth'
import { t } from '../i18n'

const router = useRouter()
const { state: auth } = useAuth()
const open = ref(false)
const query = ref('')
const input = ref(null)
const list = ref(null)
const active = ref(0)
const agents = ref([])
const tasks = ref([])
const showNew = ref(false)
const dispatchTo = ref('')
const searchError = ref('')
let seq = 0
let returnFocus = null

const PAGES = [
  { label: t('协作概览'), name: 'overview', words: 'overview 概览 首页 dashboard' },
  { label: t('对话'), name: 'chat', words: 'chat 对话 私聊 群聊 消息' },
  { label: t('智能体'), name: 'agents', words: 'agents 智能体 agent' },
  { label: t('任务中心'), name: 'tasks', words: 'tasks 任务' },
  { label: t('工作流'), name: 'workflows', words: 'workflow 工作流 流水线 编排' },
  { label: t('接入配置'), name: 'connect', words: 'connect 接入 连接器 令牌 token' },
  { label: t('免审批设置'), name: 'rules', words: 'rules 免审批 规则' },
  { label: t('审计日志'), name: 'audit', words: 'audit 审计 日志' },
  { label: t('我的'), name: 'profile', words: 'profile 我的 密码 名称' },
]

const results = computed(() => {
  const q = query.value.trim().toLowerCase()
  const items = []
  const pages = PAGES.filter(p => !q || p.label.toLowerCase().includes(q) || p.words.includes(q))
  for (const p of pages.slice(0, q ? 4 : 9)) {
    items.push({ group: t('页面'), key: 'p:' + p.name, icon: Compass, label: p.label, run: () => router.push({ name: p.name }) })
  }
  const usable = a => a.access === 'owner' || a.access === 'accepted' || auth.user?.is_admin
  const matched = agents.value.filter(a => !q || [a.address, a.name, a.owner_name, a.hostname, ...(a.tags || [])].join(' ').toLowerCase().includes(q))
  for (const a of matched.slice(0, 6)) {
    const load = a.load ? t('运行 {running} · 排队 {queued}', { running: a.load.running, queued: a.load.queued }) : ''
    items.push({
      group: t('智能体'), key: 'a:' + a.id, icon: Bot, label: `${a.owner_name} / ${a.name}`, hint: a.address,
      meta: a.online ? load : t('离线'), dim: !a.online,
      run: () => router.push({ name: 'chat', query: { agent: a.id } }),
    })
    if (usable(a)) {
      items.push({
        group: t('智能体'), key: 'd:' + a.id, icon: Send, label: t('派任务给 {agent}', { agent: a.name }), hint: a.address, sub: true,
        run: () => { dispatchTo.value = a.address; showNew.value = true },
      })
    }
  }
  for (const task of tasks.value.slice(0, 8)) {
    items.push({
      group: t('任务'), key: 't:' + task.id, icon: ListTodo, label: `#${task.id} ${task.title}`, hint: task.agent_address, status: task.status,
      meta: task.wait?.text || '',
      run: () => router.push({ name: 'task-detail', params: { id: task.id } }),
    })
  }
  if (q && 'workflow 工作流'.includes(q)) {
    items.push({ group: t('操作'), key: 'w:new', icon: Workflow, label: t('启动工作流'), run: () => router.push({ name: 'workflows', query: { new: 1 } }) })
  }
  return items
})

const grouped = computed(() => {
  const out = []
  results.value.forEach((item, index) => {
    if (!out.length || out[out.length - 1].group !== item.group) out.push({ group: item.group, items: [] })
    out[out.length - 1].items.push({ ...item, index })
  })
  return out
})

async function search() {
  const mine = ++seq
  const q = query.value.trim()
  try {
    const [a, t] = await Promise.all([
      agents.value.length ? Promise.resolve({ agents: agents.value }) : api('/api/agents'),
      api(`/api/tasks?limit=8${q ? '&q=' + encodeURIComponent(q) : ''}`),
    ])
    if (mine !== seq) return
    agents.value = a.agents
    tasks.value = t.tasks
    searchError.value = ''
  } catch (e) {
    // pages still work; say that agents/tasks could not be searched, and don't show old results as if current
    if (mine === seq) {
      searchError.value = e.message
      agents.value = []
      tasks.value = []
    }
  }
}

let timer = null
watch(query, () => {
  active.value = 0
  clearTimeout(timer)
  timer = setTimeout(search, 150)
})

function show() {
  returnFocus = document.activeElement
  open.value = true
  query.value = ''
  agents.value = []
  active.value = 0
  search()
  nextTick(() => input.value?.focus())
}

function choose(item) {
  if (!item) return
  open.value = false
  item.run()
}

function onKey(e) {
  const n = results.value.length
  if (e.key === 'ArrowDown') { e.preventDefault(); active.value = n ? (active.value + 1) % n : 0 }
  else if (e.key === 'ArrowUp') { e.preventDefault(); active.value = n ? (active.value - 1 + n) % n : 0 }
  else if (e.key === 'Enter') { e.preventDefault(); choose(results.value[active.value]) }
  else if (e.key === 'Escape') { open.value = false }
  else return
  nextTick(() => list.value?.querySelector('.palette-item.active')?.scrollIntoView({ block: 'nearest' }))
}

function onGlobal(e) {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    if (open.value) open.value = false
    else show()
  }
}
// give focus back to where it was (the trigger button, a field) when the palette closes without navigating
watch(open, visible => {
  if (!visible && returnFocus && document.contains(returnFocus)) nextTick(() => returnFocus?.focus?.())
})
onMounted(() => window.addEventListener('keydown', onGlobal))
onBeforeUnmount(() => window.removeEventListener('keydown', onGlobal))
defineExpose({ show })
</script>

<template>
  <n-modal v-model:show="open" class="palette-modal" :auto-focus="false" transform-origin="center">
    <div class="palette" role="dialog" :aria-label="t('命令面板')">
      <div class="palette-input">
        <Search :size="18" />
        <input
          ref="input"
          v-model="query"
          :placeholder="t('搜索页面、智能体、任务编号或标题…')"
          :aria-label="t('搜索')"
          role="combobox"
          aria-expanded="true"
          aria-controls="palette-list"
          :aria-activedescendant="results[active] ? 'pi-' + results[active].key : undefined"
          @keydown="onKey"
        >
        <kbd>Esc</kbd>
      </div>
      <div id="palette-list" ref="list" class="palette-list" role="listbox">
        <p v-if="searchError" class="palette-error">{{ t('搜索智能体和任务失败（{error}），下面只列出页面', { error: searchError }) }}</p>
        <p v-if="!results.length" class="palette-empty">{{ t('没有匹配的结果，换个关键词试试') }}</p>
        <section v-for="g in grouped" :key="g.group">
          <h3>{{ g.group }}</h3>
          <button
            v-for="item in g.items"
            :id="'pi-' + item.key"
            :key="item.key"
            type="button"
            role="option"
            :aria-selected="item.index === active"
            :class="['palette-item', { active: item.index === active, sub: item.sub, dim: item.dim }]"
            @mouseenter="active = item.index"
            @click="choose(item)"
          >
            <component :is="item.icon" :size="16" class="palette-item__icon" />
            <span class="palette-item__label">{{ item.label }}</span>
            <small v-if="item.hint" class="palette-item__hint">{{ item.hint }}</small>
            <StatusTag v-if="item.status" :status="item.status" />
            <small v-if="item.meta" class="palette-item__meta">{{ item.meta }}</small>
            <CornerDownLeft v-if="item.index === active" :size="14" class="palette-item__enter" />
          </button>
        </section>
      </div>
      <footer class="palette-foot"><span><kbd>↑</kbd><kbd>↓</kbd> {{ t('选择') }}</span><span><kbd>Enter</kbd> {{ t('打开') }}</span><span><kbd>⌘</kbd><kbd>K</kbd> {{ t('随时唤起') }}</span></footer>
    </div>
  </n-modal>
  <NewTaskModal v-model:show="showNew" :agent="dispatchTo" />
</template>
