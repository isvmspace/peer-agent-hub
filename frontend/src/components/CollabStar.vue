<script setup>
// 本机协作的星图：每个智能体一颗星（Claude 的子代理单独一颗），正在干活的会呼吸；
// 连线带箭头和次数，最近派过活、或接活方还在干活的连线上有光点从派活方流向接活方。旁边一栏实时播报最新动态。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { t } from '../i18n'

const props = defineProps({
  nodes: { type: Object, default: () => ({}) },
  edges: { type: Array, default: () => [] },
  events: { type: Array, default: () => [] },
  now: { type: Number, default: 0 },
  activePairs: { type: Object, default: () => ({}) }, // "from>to" whose receiving session is still working
})
const emit = defineEmits(['open'])

const HOT_FOR = 120 // seconds a line keeps flowing after a delegation
const COLORS = { claude: '#f08a4b', codex: '#5b9bff', hermes: '#b388ff', opencode: '#34d6a6', subagent: '#ffb86b' }
const NAMES = { claude: 'Claude', codex: 'Codex', hermes: 'Hermes', opencode: 'OpenCode', subagent: t('子代理') }
const color = k => COLORS[k] || '#9aa6bd'
const name = k => NAMES[k] || k

// the canvas follows the space it gets: a phone gets its own, taller layout instead of a shrunken desktop one
const root = ref(null)
const narrow = ref(false)
let observer = null
onMounted(() => {
  observer = new ResizeObserver(([entry]) => { narrow.value = entry.contentRect.width < 560 })
  observer.observe(root.value)
})
onBeforeUnmount(() => observer?.disconnect())
const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches

const box = computed(() => narrow.value ? { w: 360, h: 340, rx: 128, ry: 120, r: 30, rc: 36 } : { w: 620, h: 420, rx: 220, ry: 150, r: 34, rc: 42 })

// the node that hands out the most work sits in the middle, the others around it
const layout = computed(() => {
  const { w, h, rx, ry, r } = box.value
  const kinds = Object.keys(props.nodes)
  const out = {}
  for (const e of props.edges) out[e.from] = (out[e.from] || 0) + e.count
  const center = [...kinds].sort((a, b) => (out[b] || 0) - (out[a] || 0) || props.nodes[b].sessions - props.nodes[a].sessions)[0]
  const ring = kinds.filter(k => k !== center)
  const pos = {}
  if (center) pos[center] = { x: w / 2, y: h / 2 + 10, center: true }
  ring.forEach((k, i) => {
    const angle = -Math.PI / 2 + (2 * Math.PI * i) / Math.max(ring.length, 1)
    // kept inside the canvas whatever the angle
    const x = Math.min(w - r - 6, Math.max(r + 6, w / 2 + Math.cos(angle) * rx))
    pos[k] = { x, y: h / 2 + 10 + Math.sin(angle) * ry }
  })
  return pos
})
const radius = k => (layout.value[k]?.center ? box.value.rc : box.value.r)

const recentLaunch = computed(() => {
  const hot = {}
  for (const ev of props.events) {
    if (ev.kind !== 'launch' || props.now - ev.ts > HOT_FOR) continue
    for (const t of ev.to) hot[`${ev.from_agent}>${t.agent}`] = true
  }
  return hot
})

const paths = computed(() => props.edges.filter(e => layout.value[e.from] && layout.value[e.to]).map(e => {
  const a = layout.value[e.from]
  const b = layout.value[e.to]
  const pair = `${e.from}>${e.to}`
  let d, lx, ly
  if (e.from === e.to) { // a node handing work to its own kind: a loop above it
    const r = radius(e.from)
    d = `M${a.x - 12},${a.y - r} C${a.x - 60},${a.y - r - 80} ${a.x + 60},${a.y - r - 80} ${a.x + 12},${a.y - r}`
    lx = a.x
    ly = a.y - r - 62
  } else {
    // from edge to edge of the two circles, bent a little, so the arrow head is not hidden under the node
    const dx = b.x - a.x
    const dy = b.y - a.y
    const len = Math.hypot(dx, dy) || 1
    const ux = dx / len
    const uy = dy / len
    const sx = a.x + ux * (radius(e.from) + 4)
    const sy = a.y + uy * (radius(e.from) + 4)
    const ex = b.x - ux * (radius(e.to) + 8)
    const ey = b.y - uy * (radius(e.to) + 8)
    const cx = (sx + ex) / 2 - uy * len * 0.12
    const cy = (sy + ey) / 2 + ux * len * 0.12
    d = `M${sx},${sy} Q${cx},${cy} ${ex},${ey}`
    lx = (sx + 2 * cx + ex) / 4
    ly = (sy + 2 * cy + ey) / 4
  }
  const hot = !!(recentLaunch.value[pair] || props.activePairs[pair])
  return { ...e, id: `edge-${e.from}-${e.to}`, d, hot, lx, ly, width: Math.min(5, 1.5 + Math.log2(e.count + 1)) }
}))

const latestLaunch = (from, to) => props.events.find(ev => ev.kind === 'launch' && ev.from_agent === from && ev.to.some(t => t.agent === to))
const openable = e => !!latestLaunch(e.from, e.to)
function openEdge(e) {
  const ev = latestLaunch(e.from, e.to)
  if (ev) emit('open', ev.from, ev.to.find(t => t.agent === e.to)?.key)
}

const ticker = computed(() => props.events.slice(0, 7))
const clock = t => new Date(t * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
const working = computed(() => Object.values(props.nodes).reduce((n, v) => n + (v.active ? 1 : 0), 0))
</script>

<template>
  <div ref="root" :class="['star', { narrow }]">
    <div class="live"><i :class="{ on: working }" />{{ working ? t('实时 · {n} 个智能体正在工作', { n: working }) : t('实时 · 暂时都在休息') }}</div>
    <div class="stage">
      <svg :viewBox="`0 0 ${box.w} ${box.h}`" preserveAspectRatio="xMidYMid meet" role="img" :aria-label="t('智能体协作星图')">
        <defs>
          <radialGradient v-for="(_, k) in nodes" :id="`glow-${k}`" :key="k" cx="50%" cy="50%" r="50%">
            <stop offset="0%" :stop-color="color(k)" stop-opacity=".45" /><stop offset="100%" :stop-color="color(k)" stop-opacity="0" />
          </radialGradient>
          <marker v-for="(_, k) in nodes" :id="`arrow-${k}`" :key="`m-${k}`" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" :fill="color(k)" />
          </marker>
        </defs>
        <g v-for="e in paths" :key="`${e.id}-${narrow}`" :class="['edge', { clickable: openable(e) }]" :tabindex="openable(e) ? 0 : -1" role="button"
          :aria-label="t('{from} 派活给 {to} {n} 次，查看讨论', { from: name(e.from), to: name(e.to), n: e.count })" @click="openEdge(e)" @keydown.enter.prevent="openEdge(e)" @keydown.space.prevent="openEdge(e)">
          <path :id="e.id" :d="e.d" :stroke="color(e.to)" :stroke-width="e.width" fill="none" :opacity="e.hot ? .95 : .4" :marker-end="`url(#arrow-${e.to})`" />
          <path :d="e.d" stroke="transparent" stroke-width="18" fill="none" />
          <circle v-if="e.hot && !reduced" r="4.5" :fill="color(e.to)" class="particle">
            <animateMotion dur="2.6s" repeatCount="indefinite"><mpath :href="`#${e.id}`" /></animateMotion>
          </circle>
          <g class="count" :transform="`translate(${e.lx},${e.ly})`">
            <rect x="-17" y="-10" width="34" height="18" rx="5" />
            <text text-anchor="middle" y="3">×{{ e.count }}</text>
          </g>
        </g>
        <g v-for="(v, k) in nodes" :key="k" class="node" :transform="`translate(${layout[k]?.x || 0},${layout[k]?.y || 0})`">
          <circle :r="radius(k) * 1.9" :fill="`url(#glow-${k})`" :opacity="v.active ? 1 : .35" />
          <circle v-if="v.active && !reduced" :r="radius(k) + 4" fill="none" :stroke="color(k)" class="pulse" />
          <circle :r="radius(k)" fill="#101624" :stroke="color(k)" stroke-width="2.5" :opacity="v.active ? 1 : .6" />
          <text y="-2" text-anchor="middle" :fill="color(k)" :class="['label', { long: name(k).length > 6 }]">{{ name(k) }}</text>
          <text y="15" text-anchor="middle" class="sub">{{ v.active ? t('{n} 个进行中', { n: v.active }) : t('{n} 个会话', { n: v.sessions }) }}</text>
        </g>
      </svg>
      <div class="legend"><span><i class="lg-flow" />{{ t('流动 = 近期派活或还在干活') }}</span><span><i class="lg-line" />{{ t('线越粗、×N 越大 = 派活越多') }}</span></div>
      <div v-if="!edges.length" class="quiet">{{ t('这段时间智能体之间还没有派活') }}</div>
    </div>
    <aside class="feed">
      <div class="feed-title">{{ t('实时动态') }}</div>
      <transition-group tag="div" name="tick" class="ticker">
        <button v-for="ev in ticker" :key="`${ev.kind}-${ev.from}-${ev.ts}`" type="button" class="tick" :style="{ borderLeftColor: color(ev.from_agent) }"
          @click="ev.kind === 'launch' ? emit('open', ev.from, ev.to[0]?.key) : emit('open', ev.to[0].key, ev.from)">
          <span class="time">{{ clock(ev.ts) }}</span>
          <template v-if="ev.kind === 'launch'">
            <b :style="{ color: color(ev.from_agent) }">{{ name(ev.from_agent) }}</b> → {{ ev.to.map(x => name(x.agent)).join(t('、')) }}
            <span v-if="ev.summary" class="what">{{ ev.summary }}</span>
          </template>
          <template v-else><b :style="{ color: color(ev.from_agent) }">{{ name(ev.from_agent) }}</b> {{ t('已回复') }} ✓ <span class="what">{{ ev.summary }}</span></template>
        </button>
      </transition-group>
      <div v-if="!ticker.length" class="feed-empty">{{ t('还没有动态') }}</div>
    </aside>
  </div>
</template>

<style scoped>
.star { position: relative; display: grid; grid-template-columns: minmax(0, 1fr) 280px; grid-template-rows: auto 1fr; gap: 0 16px; border-radius: 14px; overflow: hidden;
  background: radial-gradient(ellipse at 40% 45%, #18223d 0%, #0b0f17 72%); color: #e6ebf5; padding: 12px 14px 14px; }
.star.narrow { grid-template-columns: 1fr; }
.star::before { content: ''; position: absolute; inset: 0; pointer-events: none; opacity: .5;
  background-image: radial-gradient(1px 1px at 12% 20%, #fff8, transparent), radial-gradient(1px 1px at 68% 12%, #fff6, transparent),
  radial-gradient(1px 1px at 85% 70%, #fff5, transparent), radial-gradient(1px 1px at 30% 82%, #fff6, transparent), radial-gradient(1px 1px at 52% 60%, #fff4, transparent); }
.live { grid-column: 1 / -1; display: flex; align-items: center; gap: 6px; font-size: 13px; color: #d5dcea; height: 28px; position: relative; }
.live i { width: 8px; height: 8px; border-radius: 50%; background: #5d6b84; }
.live i.on { background: #3ccfe6; box-shadow: 0 0 8px #3ccfe6; }
.stage { position: relative; min-width: 0; }
svg { width: 100%; height: auto; display: block; max-height: 440px; }
.edge.clickable { cursor: pointer; }
.edge:focus-visible { outline: none; }
.edge:focus-visible path:first-child { stroke-width: 6; }
.particle { filter: drop-shadow(0 0 4px currentColor); }
.count rect { fill: rgba(11, 15, 23, .85); stroke: rgba(255, 255, 255, .14); }
.count text { font-size: 11px; fill: #d5dcea; font-variant-numeric: tabular-nums; }
.pulse { animation: pulse 3s ease-out infinite; transform-box: fill-box; transform-origin: center; }
@keyframes pulse { 0% { transform: scale(.9); opacity: .7 } 100% { transform: scale(1.5); opacity: 0 } }
.label { font-size: 14px; font-weight: 700; }
.sub { font-size: 11px; fill: #a9b4c8; }
.label.long { font-size: 12px; }
.narrow .label { font-size: 15px; } .narrow .label.long { font-size: 12px; } .narrow .sub { font-size: 12px; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 12px; color: #a9b4c8; margin-top: 4px; }
.legend i { display: inline-block; width: 18px; height: 3px; margin-right: 6px; vertical-align: middle; border-radius: 2px; }
.lg-flow { background: linear-gradient(90deg, transparent, #5b9bff, transparent); } .lg-line { background: #a9b4c8; height: 5px !important; }
.quiet { text-align: center; font-size: 12px; color: #a9b4c8; margin-top: 6px; }
.feed { position: relative; display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.feed-title { font-size: 12px; color: #a9b4c8; }
.ticker { display: flex; flex-direction: column; gap: 6px; }
.tick { text-align: left; font: inherit; font-size: 12px; color: #e6ebf5; background: rgba(16, 22, 36, .86); border: 1px solid #243047; border-left: 3px solid;
  border-radius: 6px; padding: 6px 8px; cursor: pointer; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; min-height: 32px; }
.tick:hover { background: rgba(28, 38, 60, .95); }
.tick:focus-visible { outline: 2px solid #3ccfe6; outline-offset: 1px; }
.time { color: #a9b4c8; margin-right: 6px; font-variant-numeric: tabular-nums; }
.what { color: #b9c6dc; margin-left: 4px; }
.feed-empty { font-size: 12px; color: #a9b4c8; }
.tick-enter-active { transition: opacity .22s ease-out, transform .22s ease-out; } .tick-enter-from { opacity: 0; transform: translateX(16px); }
@media (prefers-reduced-motion: reduce) { .pulse { animation: none; } .tick-enter-active { transition: none; } }
</style>
