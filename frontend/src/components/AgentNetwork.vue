<script setup>
// People sit on a ring, their agents orbit them, and recent tasks are drawn as
// links from whoever sent them to the agent that ran them. The viewBox is fitted
// to the content, so any number of people or agents stays inside the frame.
import { computed } from 'vue'
import { STATUS } from '../utils/format'
import { t } from '../i18n'

const props = defineProps({
  agents: { type: Array, default: () => [] },
  tasks: { type: Array, default: () => [] },
  me: { type: String, default: '' },
})
const emit = defineEmits(['open-task', 'open-tasks'])

// a conversation continued by someone else (e.g. through a share) is drawn from whoever asked its latest turn
const asker = t => t.turn_username || t.from_username
const FOLD_AT = 8 // beyond this many people, agents fold into a count on their owner
// what needs attention first: a failure is never hidden behind a running task on the same pair
const KINDS = ['bad', 'waiting', 'live', 'queued', 'done', 'muted']
const KIND_LABEL = { bad: t('失败'), waiting: t('待审批'), live: t('执行'), queued: t('排队'), done: t('完成'), muted: t('取消') }
function kindOf(status) {
  if (status === 'running') return 'live'
  if (status === 'queued' || status === 'dispatched') return 'queued' // not started yet: drawn still, not flowing
  if (status === 'pending_approval') return 'waiting'
  if (status === 'failed' || status === 'rejected') return 'bad'
  if (status === 'canceled') return 'muted'
  return 'done'
}

const graph = computed(() => {
  const owners = [...new Set([
    ...props.agents.map(a => a.address.split('/')[0]),
    ...props.tasks.map(asker),
  ])].sort((a, b) => (a === props.me ? -1 : b === props.me ? 1 : a.localeCompare(b)))
  const folded = owners.length > FOLD_AT
  const agentsOf = name => props.agents.filter(a => a.address.startsWith(name + '/'))
  const maxOwn = Math.max(1, ...owners.map(n => agentsOf(n).length))
  // orbit grows with the busiest owner so their agents never touch
  const orbit = folded ? 0 : Math.max(92, maxOwn * 26)
  const solo = owners.length < 2
  // folded: each person carries a name plus an "x/y 在线 · 内部 n" line (~150px wide, ~90px tall), so the ring
  // leaves that much room between neighbours along the (1.5:1) ellipse
  const ring = solo ? 0 : folded ? Math.max(170, (owners.length * 165) / (2 * Math.PI * 1.25))
    : Math.max(170, (owners.length * (orbit * 1.4 + 60)) / (2 * Math.PI))

  const people = owners.map((name, i) => {
    const angle = Math.PI + (i / Math.max(owners.length, 1)) * Math.PI * 2
    const own = agentsOf(name)
    return {
      name, angle, own,
      online: own.filter(a => a.online).length,
      x: Math.cos(angle) * ring * 1.5,
      y: Math.sin(angle) * ring,
    }
  })
  const byName = Object.fromEntries(people.map(p => [p.name, p]))
  const nodes = []
  if (!folded) {
    for (const p of people) {
      const n = p.own.length
      const spread = Math.min(Math.PI * 1.1, 0.42 * (n - 1) + (n > 1 ? 0.5 : 0))
      p.own.forEach((a, j) => {
        const u = n === 1 ? 0 : j / (n - 1) - 0.5
        const angle = (solo ? -Math.PI / 2 : p.angle) + u * spread
        const running = a.load?.running || 0
        const queued = a.load?.queued || 0
        const busy = !a.online ? t('离线') : running ? t('执行 {n}', { n: running }) : queued ? t('排队 {n}', { n: queued }) : t('空闲')
        nodes.push({ ...a, key: a.address.split('/')[1], owner: p, running, queued, busy,
                     x: p.x + Math.cos(angle) * orbit, y: p.y + Math.sin(angle) * orbit })
      })
    }
  }
  const target = t => (folded ? byName[t.agent_address.split('/')[0]] : nodes.find(n => n.address === t.agent_address))

  // one link per sender -> agent pair, carrying a count per state
  const pairs = new Map()
  const inner = new Map() // folded view: tasks inside one person's own agents, kept as a count on that person
  for (const t of props.tasks) {
    // an agent that delegated (task tokens record from_agent) is drawn as the source, not its owner
    const fromAgent = !folded && t.from_agent && asker(t) === t.from_username ? nodes.find(n => n.address === `${t.from_username}/${t.from_agent}`) : null
    const a = fromAgent || byName[asker(t)]
    const b = target(t)
    if (a && a === b && folded) {
      if (!inner.has(a.name)) inner.set(a.name, [])
      inner.get(a.name).push(t)
      continue
    }
    if (!a || !b || a === b) continue
    const source = fromAgent ? fromAgent.address : asker(t)
    const key = `${source}>${folded ? t.agent_address.split('/')[0] : t.agent_address}`
    if (!pairs.has(key)) pairs.set(key, { key, a, b, tasks: [] })
    pairs.get(key).tasks.push(t)
  }
  const links = [...pairs.values()].map(({ key, a, b, tasks }) => {
    const counts = {}
    for (const t of tasks) counts[t.status] = (counts[t.status] || 0) + 1
    const byKind = {}
    for (const t of tasks) byKind[kindOf(t.status)] = (byKind[kindOf(t.status)] || 0) + 1
    const kind = KINDS.find(k => byKind[k])
    // the badge names each state with its count, worst first: "失败 7 · 执行 1"
    const parts = KINDS.filter(k => byKind[k]).map(k => `${KIND_LABEL[k]} ${byKind[k]}`)
    const badge = parts.length > 3 ? `${parts.slice(0, 3).join(' · ')} …` : parts.join(' · ')
    // a single finished task needs no label; anything needing attention or several tasks get one
    const showBadge = tasks.length > 1 || ['bad', 'waiting', 'live', 'queued'].includes(kind)
    // bend to the left of the direction of travel, so A→B and B→A arc apart and their labels never stack
    const len0 = Math.hypot(b.x - a.x, b.y - a.y) || 1
    const bend = Math.min(90, len0 * 0.18)
    let mx = (a.x + b.x) / 2 + ((a.y - b.y) / len0) * bend
    let my = (a.y + b.y) / 2 + ((b.x - a.x) / len0) * bend
    if (a.owner && b.owner && a.owner === b.owner) {
      // two agents of the same person: arc outward, away from their owner and the agent labels
      const cx = (a.x + b.x) / 2 - a.owner.x
      const cy = (a.y + b.y) / 2 - a.owner.y
      const len = Math.hypot(cx, cy) || 1
      const side = a.address < b.address ? 70 : 110 // the two directions take different arcs
      mx = (a.x + b.x) / 2 + (cx / len) * side
      my = (a.y + b.y) / 2 + (cy / len) * side
    }
    // stop the line at the target's edge so its arrowhead shows instead of hiding under the node
    const r = b.owner ? 17 : 25
    const dx = mx - b.x, dy = my - b.y, dl = Math.hypot(dx, dy) || 1
    const ex = b.x + (dx / dl) * r, ey = b.y + (dy / dl) * r
    const summary = Object.entries(counts).map(([s, n]) => `${STATUS[s]?.label || s} ${n}`).join(t('，'))
    return {
      key, kind, badge, showBadge, tasks, count: tasks.length, latest: tasks[0].id,
      title: `${a.address || a.name} → ${b.address || b.name}`,
      d: `M${a.x},${a.y} Q${mx},${my} ${ex},${ey}`,
      mid: { x: (a.x + 2 * mx + b.x) / 4, y: (a.y + 2 * my + b.y) / 4 },
      label: t('{a} 派给 {b}：{n} 个任务（{summary}），最近 #{id} {title}', { a: a.address || a.name, b: b.address || b.name, n: tasks.length, summary, id: tasks[0].id, title: tasks[0].title }),
    }
  })

  // labels of neighbouring links must not cover each other (or a node's text): nudge each one
  // along its link's normal until it sits clear of the ones already placed
  for (const p of people) p.inner = inner.get(p.name) || []
  const textW = t => [...t].reduce((w, c) => w + (c.charCodeAt(0) > 255 ? 13 : 7.5), 0)
  const placed = [...people.map(p => ({ x: p.x - 26, y: p.y - 26, w: 52, h: 52 })), // the disc
                  ...people.map(p => {
                    const w = Math.max(80, textW(p.name) + 30, inner.has(p.name) ? badgeWidth(innerLine({ ...p, inner: inner.get(p.name) }, folded)) + 6 : 0)
                    return { x: p.x - w / 2, y: p.y + 26, w, h: 40 }
                  }),
                  ...nodes.map(n => ({ x: n.x - 16, y: n.y - 16, w: 32, h: 32 })),
                  ...nodes.map(n => { const w = Math.max(64, textW(n.key.slice(0, 14)) + 10); return { x: n.x - w / 2, y: n.y + 16, w, h: 32 } })]
  const clash = r => placed.some(o => r.x < o.x + o.w && o.x < r.x + r.w && r.y < o.y + o.h && o.y < r.y + r.h)
  for (const l of links) {
    if (!l.showBadge) continue
    const w = badgeWidth(l.badge) + 6, h = 24
    // try nearby rows first, then shift sideways too; in a very crowded graph keep the least-crowded spot
    const spots = []
    for (const dx of [0, -0.6, 0.6, -1.2, 1.2]) for (let dy = 0; dy <= 8; dy++) for (const sgn of dy ? [1, -1] : [1]) spots.push([dx * w, sgn * dy * h])
    const overlap = r => placed.reduce((sum, o) => sum + Math.max(0, Math.min(r.x + r.w, o.x + o.w) - Math.max(r.x, o.x)) * Math.max(0, Math.min(r.y + r.h, o.y + o.h) - Math.max(r.y, o.y)), 0)
    let best = null
    for (const [dx, dy] of spots) {
      const r = { x: l.mid.x + dx - w / 2, y: l.mid.y + dy - h / 2, w, h }
      const cost = clash(r) ? overlap(r) : 0
      if (!best || cost < best.cost) best = { r, cost }
      if (!cost) break
    }
    l.mid = { x: best.r.x + w / 2, y: best.r.y + h / 2 }
    placed.push(best.r)
  }

  // fit the frame to what is drawn, leaving room for labels
  const pts = [...people.map(p => [p.x, p.y, 70]), ...nodes.map(n => [n.x, n.y, 46])]
  const pad = 30
  const minX = Math.min(-200, ...pts.map(([x, , r]) => x - r)) - pad
  const maxX = Math.max(200, ...pts.map(([x, , r]) => x + r)) + pad
  const minY = Math.min(-120, ...pts.map(([, y, r]) => y - r)) - pad
  const maxY = Math.max(120, ...pts.map(([, y, r]) => y + r)) + pad
  return { people, nodes, links, folded, box: [minX, minY, maxX - minX, maxY - minY] }
})

// one task opens directly; several open a list to choose from, never an arbitrary one of them
function open(link) {
  if (link.count === 1) emit('open-task', link.latest)
  else emit('open-tasks', { title: link.title, tasks: link.tasks })
}
function openInner(p) { emit('open-tasks', { title: t('{name} 的内部任务', { name: p.name }), tasks: p.inner }) }
const badgeWidth = text => 14 + [...text].reduce((w, c) => w + (c.charCodeAt(0) > 255 ? 12 : 7), 0)
const innerLine = (p, folded) => (folded && p.own.length ? t('{a}/{b} 在线 · ', { a: p.online, b: p.own.length }) : '') + t('内部 {n}', { n: p.inner.length })
const innerText = p => innerLine(p, graph.value.folded)
</script>

<template>
  <div class="net">
    <svg :viewBox="graph.box.join(' ')" class="net__svg" role="group" :aria-label="t('团队智能体协作网络')">
      <defs>
        <pattern id="net-dots" width="22" height="22" patternUnits="userSpaceOnUse">
          <circle cx="1" cy="1" r="1" class="net__dot" />
        </pattern>
      <marker v-for="k in KINDS" :id="'net-arrow-' + k" :key="k" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" markerUnits="userSpaceOnUse" orient="auto-start-reverse">
        <path d="M1,1 L9,5 L1,9" class="net__arrow" :class="k" />
      </marker>
      </defs>
      <rect :x="graph.box[0]" :y="graph.box[1]" :width="graph.box[2]" :height="graph.box[3]" fill="url(#net-dots)" />
      <line v-for="n in graph.nodes" :key="'s' + n.address" :x1="n.owner.x" :y1="n.owner.y" :x2="n.x" :y2="n.y" class="net__spoke" />
      <g
        v-for="l in graph.links"
        :key="l.key"
        class="net__link-group"
        role="button"
        tabindex="0"
        :aria-label="l.label"
        @click="open(l)"
        @keydown.enter.prevent="open(l)"
        @keydown.space.prevent="open(l)"
      >
        <title>{{ l.label }}</title>
        <path :d="l.d" class="net__hit" />
        <path :d="l.d" class="net__link" :class="l.kind" :marker-end="`url(#net-arrow-${l.kind})`" />
      </g>
      <g v-for="n in graph.nodes" :key="n.address" class="net__agent" :class="{ on: n.online, busy: n.online && n.running, waiting: n.online && !n.running && n.queued }">
        <title>{{ n.address }}（{{ n.busy }}）</title>
        <circle :cx="n.x" :cy="n.y" r="13" class="net__halo" />
        <circle :cx="n.x" :cy="n.y" r="7" class="net__core" />
        <path v-if="!n.online" :d="`M${n.x - 5},${n.y + 5} L${n.x + 5},${n.y - 5}`" class="net__off" />
        <text :x="n.x" :y="n.y + 27" text-anchor="middle" class="net__label">{{ n.key.length > 14 ? n.key.slice(0, 13) + '…' : n.key }}</text>
        <text :x="n.x" :y="n.y + 42" text-anchor="middle" class="net__state">{{ n.busy }}</text>
      </g>
      <g v-for="p in graph.people" :key="p.name" class="net__person" :class="{ me: p.name === me }">
        <circle :cx="p.x" :cy="p.y" r="21" class="net__person-disc" />
        <text :x="p.x" :y="p.y + 5" text-anchor="middle" class="net__initial">{{ p.name.slice(0, 1).toUpperCase() }}</text>
        <text :x="p.x" :y="p.y + 42" text-anchor="middle" class="net__name">{{ (p.name.length > 12 ? p.name.slice(0, 11) + '…' : p.name) + (p.name === me ? t('（我）') : '') }}</text>
        <text v-if="graph.folded && p.own.length && !p.inner.length" :x="p.x" :y="p.y + 58" text-anchor="middle" class="net__label">{{ t('{a}/{b} 在线', { a: p.online, b: p.own.length }) }}</text>
      </g>
      <!-- "内部 n" sits in the person's own line under the name (with the online count when folded), drawn above
           every person so nothing covers it -->
      <g v-for="p in graph.people.filter(x => x.inner.length)" :key="'i' + p.name" class="net__inner" role="button" tabindex="0"
         @click="openInner(p)" @keydown.enter.prevent="openInner(p)" @keydown.space.prevent="openInner(p)">
        <title>{{ t('{name} 派给自己智能体、或其智能体之间互派的任务：{n} 个', { name: p.name, n: p.inner.length }) }}</title>
        <rect :x="p.x - badgeWidth(innerText(p)) / 2" :y="p.y + 47" :width="badgeWidth(innerText(p))" height="18" rx="6" />
        <text :x="p.x" :y="p.y + 60" text-anchor="middle">{{ innerText(p) }}</text>
      </g>
      <!-- labels last: above nodes and every link's hit area, so a label always opens its own link -->
      <g v-for="l in graph.links.filter(x => x.showBadge)" :key="'b' + l.key" class="net__count net__link-group" :class="l.kind"
         role="button" tabindex="0" :aria-label="l.label" @click="open(l)" @keydown.enter.prevent="open(l)" @keydown.space.prevent="open(l)">
        <title>{{ l.label }}</title>
        <rect :x="l.mid.x - badgeWidth(l.badge) / 2" :y="l.mid.y - 10" :width="badgeWidth(l.badge)" height="20" rx="6" />
        <text :x="l.mid.x" :y="l.mid.y + 4" text-anchor="middle">{{ l.badge }}</text>
      </g>
      <text v-if="!graph.people.length" x="0" y="0" text-anchor="middle" class="net__empty">{{ t('还没有智能体接入，去「接入配置」把本机智能体接进来') }}</text>
    </svg>
    <ul class="net__legend">
      <li><i class="lg on" />{{ t('在线空闲') }}</li>
      <li><i class="lg busy" />{{ t('在线执行中') }}</li>
      <li><i class="lg off" />{{ t('离线') }}</li>
      <li><i class="lg live" />{{ t('执行中') }}</li>
      <li><i class="lg queued" />{{ t('排队 / 已下发') }}</li>
      <li><i class="lg waiting" />{{ t('等待审批') }}</li>
      <li><i class="lg done" />{{ t('已完成') }}</li>
      <li><i class="lg bad" />{{ t('失败 / 拒绝') }}</li>
      <li><i class="lg muted" />{{ t('已取消') }}</li>
      <li class="net__scope">{{ t('连线来自所有进行中的任务和你最近的 10 条任务，箭头指向执行方；标签是同一对之间各状态的任务数，点开看全部') }}</li>
    </ul>
  </div>
</template>
