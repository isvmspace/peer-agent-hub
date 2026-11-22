<script setup>
// 本机协作：自己电脑上 Claude Code、Codex、Hermes、OpenCode 之间谁派活给谁，以及每个会话的讨论和对话记录。
// 数据只在那台电脑上：每次查看由连接器现场读取，Hub 只转发，不保存。
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useDialog, useMessage } from 'naive-ui'
import PageHeader from '../components/PageHeader.vue'
import CollabStar from '../components/CollabStar.vue'
import CollabDiscussion from '../components/CollabDiscussion.vue'
import ClampText from '../components/ClampText.vue'
import { api } from '../api/client'
import { t } from '../i18n'

const NAMES = { claude: 'Claude', codex: 'Codex', hermes: 'Hermes', opencode: 'OpenCode', subagent: t('子代理') }
const AGENTS = ['claude', 'codex', 'hermes', 'opencode']
const RANGES = [{ label: t('最近 1 天'), value: 1 }, { label: t('最近 {n} 天', { n: 3 }), value: 3 }, { label: t('最近 {n} 天', { n: 7 }), value: 7 }, { label: t('最近 {n} 天', { n: 30 }), value: 30 }]
const message = useMessage()
const dialog = useDialog()

// the last overview per machine and range, kept while the console is open: coming back shows it at once
const overviewCache = window.__collabOverviewCache || (window.__collabOverviewCache = new Map())

const machines = ref([])
const machineId = ref(Number(safeGet('collab.machine')) || null)
const days = ref(3)
const agents = ref([...AGENTS])
const query = ref('')
const onlyCollab = ref(true)
const data = ref(null)
const loading = ref(false)
const problem = ref('')
const selected = ref('')
const tab = ref('talk') // talk: 协作讨论 · log: 对话记录
const detail = ref(null)
const detailLoading = ref(false)
const open = ref(new Set())
const narrow = ref(window.matchMedia('(max-width: 900px)').matches)

function safeGet(k) { try { return localStorage.getItem(k) } catch { return null } }
function safeSet(k, v) { try { localStorage.setItem(k, v) } catch { /* private window */ } }

const machine = computed(() => machines.value.find(m => m.id === machineId.value))
const machineOptions = computed(() => machines.value.map(m => ({ label: `${m.name}${!m.online ? ' ' + t('（离线）') : m.supported ? '' : t('（需升级连接器）')}`, value: m.id })))
const cacheKey = () => `${machineId.value}:${days.value}`

const time = t => t ? new Date(t * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : ''
const dur = (a, b) => { const s = Math.max(0, Math.round(b - a)); return s < 60 ? t('{n} 秒', { n: s }) : s < 3600 ? t('{n} 分钟', { n: Math.round(s / 60) }) : t('{n} 小时', { n: (s / 3600).toFixed(1) }) }
const short = p => (p || '').replace(/^\/Users\/[^/]+|^\/home\/[^/]+/, '~')
const nodeOf = s => s.node || s.agent // older connectors report no node

async function loadMachines() {
  machines.value = (await api('/api/collab/machines')).machines
  if (!machines.value.some(m => m.id === machineId.value)) {
    machineId.value = (machines.value.find(m => m.online) || machines.value[0])?.id ?? null
  }
}

async function load() {
  if (!machine.value) return
  if (!machine.value.enabled || !machine.value.supported || !machine.value.online) { data.value = null; problem.value = ''; return }
  const key = cacheKey()
  if (!data.value && overviewCache.has(key)) data.value = overviewCache.get(key)
  loading.value = !data.value
  try {
    const d = await api(`/api/collab/${machineId.value}/overview?days=${days.value}`)
    if (key !== cacheKey()) return // the machine or range changed meanwhile
    data.value = d
    overviewCache.set(key, d)
    problem.value = ''
  } catch (e) {
    if (!data.value) problem.value = e.message
  } finally {
    loading.value = false
  }
}

const byKey = computed(() => Object.fromEntries((data.value?.sessions || []).map(s => [s.key, s])))
const kidsOf = s => (s.children || []).map(k => byKey.value[k]).filter(Boolean)
const roots = computed(() => {
  const q = query.value.trim().toLowerCase()
  const match = s => !q || `${s.title} ${s.cwd}`.toLowerCase().includes(q)
  const anyIn = s => (agents.value.includes(s.agent) && match(s)) || kidsOf(s).some(anyIn)
  return (data.value?.sessions || [])
    .filter(s => !byKey.value[s.parent] && (!onlyCollab.value || s.children.length) && anyIn(s))
    .sort((a, b) => b.ended - a.ended) // the most recently active first
})
function countKids(s) {
  const counts = {}
  for (const k of kidsOf(s)) counts[nodeOf(k)] = (counts[nodeOf(k)] || 0) + 1
  return Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([a, n]) => `${NAMES[a] || a} ×${n}`)
}
// sessions that handed work out, the most recently busy first (their own activity or their helpers')
const recent = computed(() => (data.value?.sessions || []).filter(s => s.children.length).map(s => {
  const kids = kidsOf(s)
  return { ...s, kids, last: Math.max(s.ended, ...kids.map(k => k.ended)), busy: kids.some(k => k.active), counts: countKids(s) }
}).sort((a, b) => b.last - a.last).slice(0, 8))

// lines whose receiving session is still working keep flowing on the star graph
const activePairs = computed(() => {
  const pairs = {}
  for (const s of data.value?.sessions || []) {
    const p = byKey.value[s.parent]
    if (p && s.active) pairs[`${nodeOf(p)}>${nodeOf(s)}`] = true
  }
  return pairs
})

function toggle(key) {
  const next = new Set(open.value)
  next.has(key) ? next.delete(key) : next.add(key)
  open.value = next
}

let asked = 0
async function select(key, start, view) {
  if (view) tab.value = view
  if (selected.value !== key) { selected.value = key; allLaunches.value = false }
  const mine = ++asked
  detailLoading.value = true
  try {
    const qs = new URLSearchParams({ key, days: days.value })
    if (start !== undefined) qs.set('start', start)
    const d = await api(`/api/collab/${machineId.value}/session?${qs}`)
    if (mine === asked) detail.value = d // a slower answer for an earlier click is dropped
  } catch (e) {
    if (mine === asked) message.error(e.message)
  } finally {
    if (mine === asked) detailLoading.value = false
  }
}
// from the star graph, the feed or a card: open the discussion and bring the detail's header into view
const detailTop = ref(null)
const focus = ref('')
async function openTalk(key, focusKey = '') {
  focus.value = '' // clicking the same entry again jumps to it again
  await nextTick()
  focus.value = focusKey
  await select(key, undefined, 'talk')
  await nextTick()
  if (focusKey) return // the discussion scrolls to that reply itself
  detailTop.value?.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' })
}
function back() { selected.value = ''; detail.value = null }

// each turn: its prompt, the calls made until the next prompt, then the reply — newest turn first
const turns = computed(() => {
  const d = detail.value
  if (!d) return []
  const list = d.turns.length ? d.turns : [{ index: 0, ts: d.started, prompt: '', reply: '' }]
  return list.map((t, i) => {
    const next = list[i + 1]?.ts ?? Infinity
    const first = i === 0 && d.start === 0 // calls before the first prompt belong to it
    return { ...t, calls: d.calls.filter(c => (first || c.ts >= t.ts - 1) && c.ts < next - 1) }
  }).reverse()
})
const allLaunches = ref(false)
const shownLaunches = computed(() => {
  const list = [...(detail.value?.launches || [])].reverse() // newest first
  return allLaunches.value ? list : list.slice(0, 5)
})
const launchedIn = t => [...new Set(t.calls.flatMap(c => c.children.map(nodeOf)))]

async function setEnabled(enabled) {
  const apply = async () => {
    try {
      await api(`/api/collab/${machineId.value}`, { method: 'PUT', body: { enabled } })
      overviewCache.delete(cacheKey())
      await loadMachines()
      data.value = null
      back()
      await load()
    } catch (e) {
      message.error(e.message)
    }
  }
  if (enabled) return apply()
  dialog.warning({
    title: t('关闭这台电脑的本机协作？'), positiveText: t('关闭'), negativeText: t('取消'), onPositiveClick: apply,
    content: t('关闭后 Hub 不再向这台电脑查询，连接器也不会读取智能体的会话日志。随时可以再打开。'),
  })
}

watch([machineId, days], () => { safeSet('collab.machine', machineId.value); data.value = null; back(); load() })

let timer = null
const media = window.matchMedia('(max-width: 900px)')
const onMedia = e => { narrow.value = e.matches }
onMounted(async () => {
  media.addEventListener('change', onMedia)
  try { await loadMachines() } catch (e) { message.error(e.message) }
  await load()
  timer = setInterval(() => { if (!document.hidden) load() }, 3000) // live: new delegations show within seconds
})
onBeforeUnmount(() => { clearInterval(timer); media.removeEventListener('change', onMedia) })
</script>

<template>
  <div class="collab-page">
    <PageHeader :title="t('本机协作')" :description="t('自己电脑上各智能体之间谁给谁派了活，以及每个会话的讨论和对话记录。数据只在那台电脑上，查看时由连接器现场读取，Hub 不保存。')">
      <template #actions>
        <div class="head-actions">
          <n-select v-model:value="machineId" :options="machineOptions" size="small" :placeholder="t('选择电脑')" />
          <n-select v-model:value="days" :options="RANGES" size="small" />
          <label v-if="machine" class="switch-label"><n-switch :value="machine.enabled" size="small" @update:value="setEnabled" />
            {{ machine.enabled ? t('已开启') : t('已关闭') }}</label>
        </div>
      </template>
    </PageHeader>

    <n-empty v-if="!machines.length" :description="t('你还没有接入连接器的电脑')" />
    <n-card v-else-if="machine && !machine.enabled" class="state-card">
      <n-empty :description="t('这台电脑的本机协作已关闭：Hub 不会查询，连接器也不读取会话日志')">
        <template #extra><n-button type="primary" @click="setEnabled(true)">{{ t('打开') }}</n-button></template>
      </n-empty>
    </n-card>
    <n-card v-else-if="machine && !machine.online" class="state-card"><n-empty :description="t('这台电脑不在线，数据只在它本机')" /></n-card>
    <n-card v-else-if="machine && !machine.supported" class="state-card">
      <n-empty :description="t('这台电脑的连接器是 {v}，升级到最新版才能查看（连接器会自动升级）', { v: machine.version })" />
    </n-card>
    <n-alert v-else-if="problem" type="error" :show-icon="false">{{ problem }}</n-alert>

    <!-- skeleton while the first overview is read: the page shows its shape at once -->
    <div v-else-if="loading" class="skeleton" aria-busy="true">
      <div class="sk-star"><span class="sk-note">{{ t('正在读取这台电脑上的智能体会话…') }}</span></div>
      <div class="sk-cards"><n-skeleton v-for="n in 4" :key="n" height="92px" :sharp="false" /></div>
    </div>

    <template v-else-if="data">
      <CollabStar v-show="!(narrow && selected)" :nodes="data.nodes || {}" :edges="data.edges" :events="data.events || []" :now="data.now || 0" :active-pairs="activePairs" @open="openTalk" />

      <section v-if="recent.length && !(narrow && selected)" class="recent">
        <h3 class="section-title">{{ t('最近的协作') }} <small>{{ t('像群聊一样看它们怎么讨论') }}</small></h3>
        <div class="recent-grid">
          <button v-for="r in recent" :key="r.key" type="button" :class="['collab-card', { busy: r.busy, sel: selected === r.key }]" @click="openTalk(r.key)">
            <span class="cc-top"><span :class="['badge', 'a-' + nodeOf(r)]">{{ NAMES[nodeOf(r)] }}</span>
              <span v-if="r.busy" class="cc-live"><i />{{ t('进行中') }}</span><span class="cc-time">{{ time(r.last) }}</span></span>
            <span class="cc-title">{{ r.title }}</span>
            <span class="cc-kids">{{ t('派给') }} {{ r.counts.slice(0, 2).join(' · ') }}<template v-if="r.counts.length > 2"> · {{ t('另 {n} 类', { n: r.counts.length - 2 }) }}</template></span>
            <span class="cc-go">{{ t('查看讨论 →') }}</span>
          </button>
        </div>
      </section>

      <h3 v-show="!(narrow && selected)" class="section-title">{{ t('会话明细') }}</h3>
      <div v-show="!(narrow && selected)" class="filters">
        <n-checkbox-group v-model:value="agents"><n-space size="small">
          <n-checkbox v-for="a in AGENTS" :key="a" :value="a" :label="NAMES[a]" />
        </n-space></n-checkbox-group>
        <n-input v-model:value="query" size="small" clearable :placeholder="t('搜索标题或目录')" class="search" />
        <n-checkbox v-model:checked="onlyCollab">{{ t('只看有协作的') }}</n-checkbox>
      </div>

      <div :class="['split', { 'show-detail': narrow && selected }]">
        <aside class="tree">
          <n-empty v-if="!roots.length" size="small" :description="onlyCollab ? t('这段时间没有智能体之间的协作，取消「只看有协作的」可以看全部会话') : t('没有会话')" />
          <div v-for="s in roots" :key="s.key" class="tree-item">
            <div class="node-row">
              <button type="button" :class="['node', { sel: selected === s.key }]" @click="select(s.key)">
                <span :class="['badge', 'a-' + nodeOf(s)]">{{ NAMES[nodeOf(s)] || s.agent }}</span>
                <span class="title" :title="s.title">{{ s.title }}</span>
                <span class="meta">
                  <span>{{ t('最近 {time}', { time: time(s.ended) }) }}</span><span v-if="s.origin">{{ t(s.origin) }}</span>
                  <span v-if="s.children.length">{{ t('派出 {list}', { list: countKids(s).join(t('、')) }) }}</span>
                </span>
              </button>
              <button v-if="s.children.length > 4" type="button" class="fold" :aria-expanded="open.has(s.key)" :aria-label="open.has(s.key) ? t('收起子会话') : t('展开子会话')"
                @click="toggle(s.key)">{{ open.has(s.key) ? '−' : '+' }}</button>
            </div>
            <div v-if="s.children.length && (s.children.length <= 4 || open.has(s.key))" class="kids">
              <button v-for="k in kidsOf(s)" :key="k.key" type="button" :class="['node', { sel: selected === k.key }]" @click="select(k.key)">
                <span :class="['badge', 'a-' + nodeOf(k)]">{{ NAMES[nodeOf(k)] || k.agent }}</span>
                <span class="title" :title="k.title">{{ k.title }}</span>
                <span class="meta"><span>{{ time(k.started) }}</span><span v-if="k.origin">{{ t(k.origin) }}</span>
                  <span v-if="k.link === 'inferred'" class="tag-inferred" :title="t('按时间和目录推断出的派活关系')">{{ t('推断') }}</span>
                  <span v-if="k.children.length">{{ t('派出 {list}', { list: countKids(k).join(t('、')) }) }}</span></span>
              </button>
            </div>
          </div>
        </aside>

        <main ref="detailTop" class="detail">
          <n-button v-if="narrow && selected" class="back" quaternary @click="back">{{ t('← 返回会话列表') }}</n-button>
          <n-empty v-if="!selected" :description="t('选择左侧的会话，看它和其他智能体的讨论与对话记录')" class="detail-empty" />
          <div v-else-if="!detail" class="detail-loading"><n-spin size="small" /> {{ t('正在读取这个会话…') }}</div>
          <div v-else :class="['detail-body', { stale: detailLoading }]">
            <div class="dhead">
              <div class="dhead-top"><span :class="['badge', 'a-' + nodeOf(detail)]">{{ NAMES[nodeOf(detail)] }}</span>
                <span v-if="detail.origin" class="chip">{{ t(detail.origin) }}</span>
                <span v-if="detailLoading" class="muted">{{ t('更新中…') }}</span></div>
              <h3>{{ detail.title }}</h3>
              <div class="dmeta">
                <span>{{ t('{time} 开始，持续 {dur}', { time: time(detail.started), dur: dur(detail.started, detail.ended) }) }}</span><span>{{ t('目录 {dir}', { dir: short(detail.cwd) }) }}</span>
                <span>{{ t('{n} 轮对话 · {m} 次工具调用', { n: detail.total_turns, m: detail.tools }) }}</span>
              </div>
              <div v-if="detail.parent_brief" class="parent">{{ t('由') }} <span :class="['badge', 'a-' + nodeOf(detail.parent_brief)]">{{ NAMES[nodeOf(detail.parent_brief)] }}</span>
                <button type="button" class="link" @click="select(detail.parent_brief.key)">{{ detail.parent_brief.title }}</button> {{ t('派出') }}
                <span class="chip">{{ detail.link === 'inferred' ? t('按时间和目录推断') : t('智能体自己记录') }}</span>
              </div>
            </div>

            <n-tabs v-model:value="tab" type="line" size="small" class="detail-tabs">
              <n-tab-pane name="talk" :tab="t('协作讨论') + (detail.launches.length ? t(' · {n} 次派活', { n: detail.launches.length }) : '')">
                <CollabDiscussion :machine-id="machineId" :session-key="selected" :days="days" :focus="focus" @session="k => select(k, undefined, 'log')" />
              </n-tab-pane>
              <n-tab-pane name="log" :tab="t('对话记录 · {n} 轮', { n: detail.total_turns })">
                <section v-if="detail.launches.length" class="panel">
                  <div class="panel-head"><span>{{ t('派出的任务') }} <small>{{ t('{n} 次 · 最新在上', { n: detail.launches.length }) }}</small></span></div>
                  <div v-for="l in shownLaunches" :key="l.call" class="launch-row">
                    <span class="lr-sum" :title="l.summary">{{ l.summary || t('（没有说明）') }}</span>
                    <span class="lr-meta">
                      <span>{{ time(l.ts) }}</span>
                      <span v-for="c in l.children" :key="c.key" :class="['badge', 'a-' + nodeOf(c)]">{{ NAMES[nodeOf(c)] }}</span>
                      <button type="button" class="link" @click="select(selected, l.turn, 'log')">{{ t('查看第 {n} 轮', { n: l.turn + 1 }) }}</button>
                    </span>
                  </div>
                  <button v-if="detail.launches.length > 5" type="button" class="link more" @click="allLaunches = !allLaunches">{{ allLaunches ? t('收起') : t('查看全部 {n} 次', { n: detail.launches.length }) }}</button>
                </section>

                <section class="panel">
                  <div class="panel-head">
                    <span>{{ t('对话记录') }} <small>{{ t('最新在上 · 第 {a}–{b} 轮，共 {n} 轮', { a: detail.start + 1, b: detail.end, n: detail.total_turns }) }}</small></span>
                    <button v-if="detail.end < detail.total_turns" type="button" class="link" @click="select(selected)">{{ t('回到最新') }}</button>
                  </div>
                  <article v-for="turn in turns" :key="turn.index" class="turn-card">
                    <header class="turn-head">{{ t('第 {n} 轮', { n: turn.index + 1 }) }}<span class="muted"> · {{ time(turn.ts) }}</span>
                      <span v-for="a in launchedIn(turn)" :key="a" :class="['badge', 'a-' + a]" :title="t('这一轮派出的智能体')">→ {{ NAMES[a] }}</span>
                    </header>
                    <div v-if="turn.prompt" class="part"><span class="part-label">{{ t('提问') }}</span><div class="bubble prompt"><ClampText :text="turn.prompt" /></div></div>
                    <details v-if="turn.calls.length" class="steps" :open="turn.calls.some(c => c.children.length)">
                      <summary>{{ t('执行过程：{n} 次工具调用', { n: turn.calls.length }) }}<span v-if="launchedIn(turn).length">{{ t('，其中派活给 {list}', { list: launchedIn(turn).map(a => NAMES[a]).join(t('、')) }) }}</span></summary>
                      <details v-for="c in turn.calls" :key="c.id" :class="['call', { launch: c.launches.length }]" :open="c.children.length > 0">
                        <summary><code>{{ c.tool }}</code><span class="sum">{{ c.summary || c.command }}</span>
                          <span v-if="c.launches.length" class="chip warn">{{ t('启动 {list}', { list: c.launches.map(a => NAMES[a] || a).join(t('、')) }) }}</span></summary>
                        <div v-for="k in c.children" :key="k.key" class="row">
                          <span :class="['badge', 'a-' + nodeOf(k)]">{{ NAMES[nodeOf(k)] }}</span><button type="button" class="link sum" @click="select(k.key)">{{ k.title }}</button><span class="muted">{{ time(k.started) }}</span>
                        </div>
                        <pre v-if="c.command">{{ c.command }}</pre>
                        <pre v-if="c.result">{{ c.result }}</pre>
                      </details>
                    </details>
                    <div v-if="turn.reply" class="part"><span class="part-label">{{ t('回复') }}</span><div class="bubble reply"><ClampText :text="turn.reply" markdown /></div></div>
                    <div v-else-if="!turn.calls.length && !turn.prompt" class="muted">{{ t('（这一轮没有文字内容）') }}</div>
                  </article>
                  <n-button v-if="detail.start > 0" block secondary size="small" @click="select(selected, Math.max(0, detail.start - 20), 'log')">{{ t('加载更早的 {n} 轮', { n: 20 }) }}</n-button>
                </section>

                <section v-if="detail.other_children.length" class="panel">
                  <div class="panel-head"><span>{{ t('其他派出的会话') }}</span></div>
                  <div v-for="k in detail.other_children" :key="k.key" class="row">
                    <span :class="['badge', 'a-' + nodeOf(k)]">{{ NAMES[nodeOf(k)] }}</span><button type="button" class="link sum" @click="select(k.key)">{{ k.title }}</button>
                  </div>
                </section>
              </n-tab-pane>
            </n-tabs>
          </div>
        </main>
      </div>
    </template>
  </div>
</template>

<style scoped>
.collab-page { display: flex; flex-direction: column; gap: 16px; }
.head-actions { display: grid; grid-template-columns: 200px 120px auto; gap: 8px; align-items: center; }
.switch-label { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; white-space: nowrap; color: var(--ink-2); }
.state-card { max-width: 720px; }
.muted { color: var(--ink-3); }
.section-title { margin: 8px 0 0; font-size: 15px; color: var(--ink); }
.section-title small { font-weight: 400; color: var(--ink-3); margin-left: 6px; font-size: 12px; }

/* skeleton */
.skeleton { display: flex; flex-direction: column; gap: 16px; }
.sk-star { height: 360px; border-radius: 14px; display: grid; place-items: center; background: radial-gradient(ellipse at 50% 45%, #18223d 0%, #0b0f17 72%); }
.sk-note { color: #c7d0e0; font-size: 13px; }
.sk-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 12px; }

/* agent colours: dark text on the light ones keeps them readable */
.badge { display: inline-block; font-size: 11px; font-weight: 600; border-radius: 4px; padding: 0 6px; line-height: 18px; flex: 0 0 auto; color: #fff; }
.a-claude { background: #b65a24; } .a-codex { background: #2a63c9; } .a-hermes { background: #7446cf; } .a-opencode { background: #13785c; }
.a-subagent { background: #f2b05a; color: #2a1a05; }
.chip { font-size: 11px; color: var(--ink-2); border: 1px solid var(--line); border-radius: 4px; padding: 0 6px; line-height: 18px; }
.chip.warn { color: #9a5b00; border-color: #e0a84a; background: rgba(224, 168, 74, .12); }
.tag-inferred { font-size: 11px; color: #9a5b00; border: 1px solid #e0a84a; border-radius: 4px; padding: 0 5px; }
.link { padding: 0; border: 0; background: none; font: inherit; color: var(--signal-strong); cursor: pointer; text-align: left; }
.link:hover { text-decoration: underline; }
.link:focus-visible, .node:focus-visible, .collab-card:focus-visible, .fold:focus-visible { outline: 2px solid var(--signal); outline-offset: 2px; }

/* recent collaborations */
.recent-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; margin-top: 10px; }
.collab-card { display: flex; flex-direction: column; gap: 6px; text-align: left; padding: 12px; border-radius: 10px; cursor: pointer; font: inherit; color: var(--ink);
  border: 1px solid var(--line); background: var(--surface); transition: transform .15s, border-color .15s; min-height: 44px; }
.collab-card:hover { transform: translateY(-2px); border-color: var(--signal); }
.collab-card.busy { border-color: color-mix(in srgb, var(--flow) 45%, var(--line)); }
.collab-card.sel { border-color: var(--signal); background: var(--signal-soft); }
.cc-top { display: flex; align-items: center; gap: 6px; font-size: 12px; }
.cc-live { display: inline-flex; align-items: center; gap: 4px; color: var(--flow); font-weight: 600; }
.cc-live i { width: 7px; height: 7px; border-radius: 50%; background: var(--flow); }
.cc-time { margin-left: auto; color: var(--ink-3); }
.cc-title { font-size: 14px; font-weight: 600; line-height: 1.5; min-height: 42px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.cc-kids { font-size: 12px; color: var(--ink-2); }
.cc-go { font-size: 12px; color: var(--signal-strong); }

/* filters and tree */
.filters { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }
.search { max-width: 240px; }
.split { display: grid; grid-template-columns: minmax(280px, 380px) minmax(0, 1fr); gap: 16px; }
.tree { overflow-y: auto; max-height: calc(100vh - 240px); border-right: 1px solid var(--line-soft); padding-right: 8px; }
.tree-item { margin-bottom: 2px; }
.node-row { display: flex; align-items: flex-start; gap: 4px; }
.node { display: grid; grid-template-columns: 56px minmax(0, 1fr); gap: 2px 8px; width: 100%; text-align: left; padding: 8px; border-radius: 8px;
  border: 1px solid transparent; background: transparent; color: var(--ink); font: inherit; cursor: pointer; min-height: 44px; }
.node .badge { justify-self: start; align-self: start; margin-top: 1px; }
.node:hover { background: var(--surface-2); }
.node.sel { border-color: var(--signal); background: var(--signal-soft); }
.node .title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.node .meta { grid-column: 2; display: flex; flex-wrap: wrap; gap: 4px 8px; align-items: center; font-size: 12px; color: var(--ink-3); }
.fold { flex: none; width: 32px; height: 32px; margin-top: 6px; border-radius: 6px; border: 1px solid var(--line); background: var(--surface); color: var(--ink-2); cursor: pointer; font-size: 16px; }
.kids { margin-left: 16px; border-left: 2px solid var(--line); padding-left: 6px; }

/* detail */
.detail { min-width: 0; scroll-margin-top: 72px; }
.detail-empty { margin-top: 60px; }
.detail-loading { display: flex; gap: 8px; align-items: center; padding: 40px 0; color: var(--ink-2); }
.detail-body { transition: opacity .2s; }
.detail-body.stale { opacity: .6; }
.dhead-top { display: flex; align-items: center; gap: 6px; }
.dhead h3 { margin: 6px 0; font-size: 17px; color: var(--ink); }
.dmeta { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 12px; color: var(--ink-3); }
.parent { margin-top: 8px; font-size: 13px; display: flex; gap: 6px; align-items: center; flex-wrap: wrap; color: var(--ink-2); }
.detail-tabs { margin-top: 12px; }
.back { margin-bottom: 8px; }
.panel { margin-top: 12px; border: 1px solid var(--line); border-radius: 10px; padding: 12px; background: var(--surface); }
.panel-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 14px; font-weight: 600; margin-bottom: 8px; color: var(--ink); }
.panel-head small { font-weight: 400; color: var(--ink-3); margin-left: 6px; font-size: 12px; }
.launch-row { display: grid; grid-template-columns: minmax(0, 1fr); gap: 4px; padding: 8px 0; border-top: 1px dashed var(--line); }
.lr-sum { font-size: 14px; line-height: 1.5; color: var(--ink); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.lr-meta { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; font-size: 12px; color: var(--ink-3); }
.more { margin-top: 6px; font-size: 12px; }
.row { display: flex; gap: 8px; align-items: center; padding: 6px 0; font-size: 13px; border-top: 1px dashed var(--line); }
.sum { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.turn-card { border-top: 1px solid var(--line-soft); padding: 14px 0; display: flex; flex-direction: column; gap: 10px; --clamp-fade: var(--surface-2); }
.turn-card:first-of-type { border-top: 0; }
.turn-head { font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; color: var(--ink); }
.part { display: grid; grid-template-columns: 36px minmax(0, 1fr); gap: 8px; align-items: start; }
.part-label { font-size: 12px; color: var(--ink-3); padding-top: 10px; }
.bubble { max-width: none; border-radius: 10px; padding: 10px 14px; font-size: 14px; line-height: 1.7; color: var(--ink); background: var(--surface-2); }
.bubble.prompt { background: var(--signal-soft); --clamp-fade: var(--signal-soft); }
.bubble.reply { border: 1px solid var(--line); }
details.steps { border: 1px dashed var(--line); border-radius: 8px; padding: 4px 10px; margin-left: 44px; }
details.steps > summary { cursor: pointer; font-size: 12px; color: var(--ink-2); padding: 6px 0; }
details.call { border: 1px solid var(--line); border-radius: 8px; margin: 6px 0; background: var(--surface); }
details.call.launch { border-color: #e0a84a; }
details summary { list-style: none; }
details summary::-webkit-details-marker { display: none; }
details > summary::before { content: '▸'; display: inline-block; margin-right: 6px; color: var(--ink-3); transition: transform .15s; }
details[open] > summary::before { transform: rotate(90deg); }
details.call > summary { display: flex; gap: 8px; align-items: center; padding: 6px 10px; cursor: pointer; font-size: 13px; color: var(--ink); }
details.call code { font-size: 12px; color: var(--ink-3); }
details.call .row { padding: 6px 10px; }
details.call pre { margin: 0; padding: 8px 10px; border-top: 1px solid var(--line-soft); white-space: pre-wrap; word-break: break-all; font-size: 12px; max-height: 240px; overflow: auto; color: var(--ink-2); }

@media (max-width: 900px) {
  .head-actions { grid-template-columns: minmax(0, 1fr) 112px; width: 100%; }
  .switch-label { grid-column: 1 / -1; }
  .split { grid-template-columns: 1fr; }
  .split .detail { display: none; }
  .split.show-detail .tree { display: none; }
  .split.show-detail .detail { display: block; }
  .tree { max-height: none; border-right: 0; padding-right: 0; }
  .part { grid-template-columns: 1fr; gap: 4px; }
  .part-label { padding-top: 0; }
  details.steps { margin-left: 0; }
  .search { max-width: none; flex: 1 1 100%; }
}
@media (prefers-reduced-motion: reduce) {
  .collab-card { transition: none; }
  .collab-card:hover { transform: none; }
}
</style>
