<script setup>
// 一个会话派出去的活，整理成群聊：每次派活是一轮，先是派活方的问题，再按完成先后排各智能体的回答；
// 还在回答的显示「正在输入」。点头像进入那个会话的详细记录。
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api/client'
import ClampText from './ClampText.vue'
import { t } from '../i18n'

const props = defineProps({
  machineId: { type: Number, required: true }, sessionKey: { type: String, default: '' }, days: { type: Number, default: 3 },
  focus: { type: String, default: '' }, // a helper session whose reply to bring into view (from the live feed or the star graph)
})
const emit = defineEmits(['session'])

const COLORS = { claude: '#f08a4b', codex: '#5b9bff', hermes: '#b388ff', opencode: '#34d6a6', subagent: '#ffb86b' }
const NAMES = { claude: 'Claude', codex: 'Codex', hermes: 'Hermes', opencode: 'OpenCode', subagent: t('子代理') }
const INITIAL = { claude: 'C', codex: 'X', hermes: 'H', opencode: 'O', subagent: t('子') }
const data = ref(null)
const error = ref('')
let timer = null
let asked = 0
let closed = false // set when the drawer goes away: a reply landing afterwards must not start polling again

const clock = t => t ? new Date(t * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : ''
const took = (a, b) => { const s = Math.max(0, Math.round(b - a)); return s < 60 ? t('{n} 秒', { n: s }) : s < 3600 ? t('{n} 分钟', { n: Math.round(s / 60) }) : t('{n} 小时', { n: (s / 3600).toFixed(1) }) }

async function load() {
  if (!props.sessionKey) return
  const mine = ++asked
  try {
    const d = await api(`/api/collab/${props.machineId}/discussion?key=${encodeURIComponent(props.sessionKey)}&days=${props.days}`)
    if (mine === asked) { data.value = d; error.value = '' } // a slower answer for an earlier session is dropped
  } catch (e) {
    if (mine === asked) error.value = e.message
  }
}

// while someone is still answering, keep listening
function schedule() {
  clearTimeout(timer)
  if (closed) return
  timer = setTimeout(async () => {
    if (!document.hidden) await load()
    if (!closed && data.value?.rounds?.some(r => r.replies.some(x => x.active))) schedule()
  }, 3000)
}

// the latest round first, so the newest discussion is right there without scrolling the page
const rounds = computed(() => (data.value?.rounds || []).map((r, i) => ({ ...r, number: i + 1 })).reverse())
const flash = ref('')
async function reveal() {
  if (!props.focus || !data.value) return
  await nextTick()
  const el = document.getElementById(`reply-${props.focus}`)
  if (!el) return
  el.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'center' })
  flash.value = props.focus
  setTimeout(() => { if (flash.value === props.focus) flash.value = '' }, 2400)
}
watch(() => props.sessionKey, async () => {
  data.value = null
  await load()
  reveal()
  schedule()
}, { immediate: true })
watch(() => props.focus, reveal)
onBeforeUnmount(() => { closed = true; clearTimeout(timer) })
</script>

<template>
  <div class="discussion">
    <n-alert v-if="error" type="error" :show-icon="false">{{ error }}</n-alert>
    <div v-else-if="!data" class="loading"><n-spin size="small" /><span>{{ t('正在读取这次协作的讨论…') }}</span></div>
    <template v-else>
      <n-empty v-if="!data.rounds.length" :description="t('这个会话还没有派活给其他智能体')" />
      <div v-if="rounds.length > 1" class="order">{{ t('共 {n} 轮派活 · 最新在上', { n: rounds.length }) }}</div>
      <template v-for="r in rounds" :key="r.call || r.number">
        <div class="round"><span>{{ t('第 {n} 轮', { n: r.number }) }} · {{ clock(r.ts) }}</span></div>
        <div class="msg">
          <button type="button" class="av" :style="{ background: COLORS[data.node] }" :title="t('查看 {title}', { title: data.title })" @click="emit('session', data.key)">{{ INITIAL[data.node] }}</button>
          <div class="body">
            <div class="who"><i class="dot" :style="{ background: COLORS[data.node] }" /><b>{{ NAMES[data.node] }}</b>{{ t('发起') }} · {{ r.to.map(a => '@' + NAMES[a]).join(' ') }}</div>
            <div class="bubble ask"><ClampText :text="r.prompt || t('（没有记录到问题原文）')" /></div>
          </div>
        </div>
        <div v-for="x in r.replies" :id="`reply-${x.key}`" :key="x.key" :class="['msg', { flash: flash === x.key }]">
          <button type="button" class="av" :style="{ background: COLORS[x.agent] }" :title="t('查看 {title}', { title: x.title })" @click="emit('session', x.key)">{{ INITIAL[x.agent] }}</button>
          <div class="body">
            <div class="who"><i class="dot" :style="{ background: COLORS[x.agent] }" /><b>{{ NAMES[x.agent] }}</b>
              <template v-if="x.active">{{ t('正在回答…') }}</template><template v-else>{{ clock(x.ended) }} · {{ t('用时 {dur}', { dur: took(x.started, x.ended) }) }}</template>
              <button type="button" class="see" @click="emit('session', x.key)">{{ t('查看会话 →') }}</button>
            </div>
            <div v-if="x.active && !x.reply" class="bubble typing"><span /><span /><span /></div>
            <div v-else class="bubble"><ClampText :text="x.reply || t('（没有文字回复）')" markdown /><div v-if="x.active" class="typing inline"><span /><span /><span /></div></div>
          </div>
        </div>
      </template>
    </template>
  </div>
</template>

<style scoped>
.discussion { display: flex; flex-direction: column; gap: 4px; }
.loading { min-height: 200px; display: grid; place-content: center; justify-items: center; gap: 10px; color: var(--ink-2); font-size: 13px; }
.order { font-size: 12px; color: var(--ink-3); }
.round { text-align: center; margin: 16px 0 6px; font-size: 12px; color: var(--ink-2); }
.round span { padding: 2px 12px; border-radius: 10px; background: var(--surface-2); }
.msg { display: flex; gap: 10px; margin: 6px 0; border-radius: 12px; transition: box-shadow .3s, background .3s; scroll-margin: 80px; }
.msg.flash { background: var(--signal-soft); box-shadow: 0 0 0 2px var(--signal); }
.av { width: 36px; height: 36px; border-radius: 50%; border: 0; flex: 0 0 auto; display: grid; place-items: center; font-weight: 700; font-size: 13px;
  color: #0b0f17; cursor: pointer; }
.av:focus-visible, .see:focus-visible { outline: 2px solid var(--signal); outline-offset: 2px; }
.body { min-width: 0; flex: 1; }
.who { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; font-size: 12px; color: var(--ink-3); margin-bottom: 4px; }
.who b { color: var(--ink); }
.dot { width: 8px; height: 8px; border-radius: 50%; }
.see { margin-left: auto; padding: 0; border: 0; background: none; font: inherit; font-size: 12px; color: var(--signal-strong); cursor: pointer; }
.bubble { max-width: none; border: 1px solid var(--line); border-radius: 4px 12px 12px 12px; padding: 10px 14px; font-size: 14px; line-height: 1.7;
  color: var(--ink); background: var(--surface); --clamp-fade: var(--surface); }
.bubble.ask { background: var(--signal-soft); --clamp-fade: var(--signal-soft); }
.typing span { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: currentColor; opacity: .5; margin-right: 4px; animation: bounce 1.2s infinite; }
.typing span:nth-child(2) { animation-delay: .15s; } .typing span:nth-child(3) { animation-delay: .3s; }
.typing.inline { margin-top: 6px; }
@keyframes bounce { 50% { transform: translateY(-4px); } }
@media (max-width: 600px) { .av { width: 44px; height: 44px; } }
@media (prefers-reduced-motion: reduce) { .typing span { animation: none; } }
</style>
