<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import { Ban, Bot, Check, ChevronDown, ChevronRight, FolderOpen, Send, Share2, X } from '@lucide/vue'
import StatusTag from './StatusTag.vue'
import TaskFiles from './TaskFiles.vue'
import ImageAttach from './ImageAttach.vue'
import ShareDialog from './ShareDialog.vue'
import { api } from '../api/client'
import { openStream } from '../api/stream'
import { ACTIVE_STATUSES, formatTime } from '../utils/format'
import { useDraft } from '../composables/useDraft'
import { useAuth } from '../composables/useAuth'

// One task rendered as a conversation: each turn is my message, the agent's
// live output (collapsible) and its final reply. Updates arrive over SSE.
import MarkdownText from './MarkdownText.vue'
import { t } from '../i18n'
// paged: open on the latest turns and load older ones on scroll (the chat page); otherwise load everything
const props = defineProps({ taskId: { type: Number, required: true }, paged: { type: Boolean, default: false } })
const emit = defineEmits(['changed', 'loaded', 'update'])
const message = useMessage()
const task = ref(null)
const events = ref([])
const { draft, clear } = useDraft(() => `task:${props.taskId}`)
const images = ref([])
const busy = ref(false)
const expanded = ref({})
const scroller = ref(null)
const showShare = ref(false)
const firstTurn = ref(1) // oldest turn loaded; earlier turns load when the reader scrolls to the top
const loadingOlder = ref(false)
const PAGE_TURNS = 5
let lastId = 0
let lastTop = 0 // older turns load only when the reader scrolls up, not on the opening jump to the bottom
let close = null

const active = computed(() => task.value && ACTIVE_STATUSES.includes(task.value.status))
const { state: auth } = useAuth()
// only the requester or the agent's owner may cancel; readers of a share or a group see no button
const canCancel = computed(() => active.value && [task.value.from_user_id, task.value.owner_user_id].includes(auth.user?.id))
const canSend = computed(() => task.value && task.value.can_continue !== false && ['completed', 'failed'].includes(task.value.status))
const turns = computed(() => {
  const map = new Map()
  for (const e of events.value) {
    if (!map.has(e.turn)) map.set(e.turn, { turn: e.turn, prompt: null, output: [], status: [], files: [], inputs: [], notes: [], result: null, error: null })
    const t = map.get(e.turn)
    if (e.kind === 'prompt') t.prompt = e
    else if (e.kind === 'result') t.result = e
    else if (e.kind === 'error') t.error = e
    else if (e.kind === 'status') t.status.push(e)
    else if (e.kind === 'image' || e.kind === 'file') {
      const f = { id: e.id, kind: e.kind, ...JSON.parse(e.content) }
      ;(f.input ? t.inputs : t.files).push(f) // images the person sent show with their message
    }
    else if (e.kind === 'usage' || e.kind === 'artifact' || e.kind === 'review') t.notes.push(e)
    else t.output.push(e)
  }
  return [...map.values()]
})

function isLive(turn) {
  return active.value && turn.turn === task.value.turn
}

// while a turn runs, the reply bubble shows the agent's latest text so far; the final result replaces it
function draftOf(turn) {
  if (!isLive(turn) || turn.result || turn.error) return ''
  const last = turn.output.findLast(e => e.kind === 'output' && e.content?.trim() && !e.content.startsWith('⚠'))
  return last ? last.content : ''
}

// a bare status word ("running", "queued") reads as 处理中; a status with a note shows the note
function liveNote(turn) {
  const s = turn.status.at(-1)?.content || ''
  const i = s.indexOf(':')
  return i > 0 && s.slice(i + 1).trim() ? s.slice(i + 1).trim() : t('处理中')
}

function showOutput(turn) {
  return expanded.value[turn.turn] ?? isLive(turn)
}

async function scrollDown(force = false) {
  await nextTick()
  const el = scroller.value
  if (el && (force || el.scrollHeight - el.scrollTop - el.clientHeight < 240)) el.scrollTop = el.scrollHeight
}

async function loadOlder() {
  if (loadingOlder.value || firstTurn.value <= 1) return
  const taskId = props.taskId
  const from = Math.max(1, firstTurn.value - PAGE_TURNS)
  loadingOlder.value = true
  try {
    const data = await api(`/api/tasks/${taskId}?turn_from=${from}&turn_to=${firstTurn.value}`)
    if (taskId !== props.taskId) return
    const el = scroller.value
    const keep = el ? el.scrollHeight - el.scrollTop : 0
    events.value.unshift(...data.events)
    firstTurn.value = from
    await nextTick()
    if (el) el.scrollTop = el.scrollHeight - keep // stay at the same message
  } catch (e) {
    message.error(e.message)
  } finally {
    loadingOlder.value = false
  }
}

function onScroll() {
  const el = scroller.value
  if (!el) return
  const up = el.scrollTop < lastTop
  lastTop = el.scrollTop
  if (up && el.scrollTop < 80) loadOlder()
}

function connect() {
  close?.()
  lastId = 0
  events.value = []
  task.value = null
  firstTurn.value = 1
  lastTop = 0
  let fresh = true
  close = openStream(
    // a fresh view gets the latest turns only; a reconnect resumes after the last event it has
    () => `/api/tasks/${props.taskId}/stream?after=${lastId}${props.paged ? `&turns=${PAGE_TURNS}` : ''}`,
    (name, data) => {
      if (name !== 'task') return
      const changed = task.value?.status !== data.task.status
      task.value = data.task
      if (fresh) firstTurn.value = data.first_turn || 1
      if (data.events.length) {
        events.value.push(...data.events)
        lastId = data.events[data.events.length - 1].id
        scrollDown(fresh) // opening a conversation lands on its latest message
      }
      fresh = false
      if (changed) emit('loaded', data.task)
      emit('update', data.task)
    },
    error => message.error(error.message),
  )
}

async function act(action, body) {
  if (busy.value) return
  const taskId = props.taskId
  busy.value = true
  try {
    await api(`/api/tasks/${taskId}/${action}`, { method: 'POST', body })
    if (action === 'follow-up') {
      clear(`task:${taskId}`, body.message)
      images.value = []
    }
    emit('changed')
  } catch (e) {
    message.error(e.message)
  } finally {
    busy.value = false
  }
}

function send() {
  if (canSend.value && (draft.value.trim() || images.value.length)) {
    act('follow-up', { message: draft.value, images: images.value.map(({ name, data }) => ({ name, data })) })
  }
}

watch(() => props.taskId, connect, { immediate: true })
onBeforeUnmount(() => close?.())
</script>

<template>
  <div class="chat-thread">
    <div v-if="task" class="chat-thread__bar">
      <div class="chat-thread__title">
        <strong>#{{ task.id }} {{ task.title }}</strong>
        <small>
          {{ task.from_username }}{{ task.from_agent ? '/' + task.from_agent : '' }} → {{ task.agent_address }}
          <span v-if="task.workspace" class="ws-chip"><FolderOpen :size="12" />{{ task.workspace }}</span>
        </small>
      </div>
      <n-space size="small" align="center">
        <StatusTag :status="task.status" />
        <n-button v-if="task.can_share" size="small" secondary @click="showShare = true"><template #icon><Share2 /></template>{{ t('分享') }}</n-button>
        <template v-if="task.can_approve">
          <n-button size="small" type="primary" :loading="busy" @click="act('approve')"><template #icon><Check /></template>{{ t('批准') }}</n-button>
          <n-button size="small" :loading="busy" @click="act('reject')"><template #icon><X /></template>{{ t('拒绝') }}</n-button>
        </template>
        <n-popconfirm v-if="canCancel" @positive-click="act('cancel')">
          <template #trigger><n-button size="small" type="error" secondary :loading="busy"><template #icon><Ban /></template>{{ t('取消') }}</n-button></template>
          {{ t('确认取消？正在执行的进程会被结束。') }}
        </n-popconfirm>
      </n-space>
    </div>

    <div ref="scroller" class="chat-thread__body" @scroll.passive="onScroll">
      <div v-if="firstTurn > 1" class="chat-older">
        <n-button size="small" quaternary :loading="loadingOlder" @click="loadOlder">{{ t('加载更早的 {n} 轮（共 {total} 轮未显示）', { n: Math.min(PAGE_TURNS, firstTurn - 1), total: firstTurn - 1 }) }}</n-button>
      </div>
      <n-alert v-if="task?.shared_by" type="info" :bordered="false" class="chat-alert">
        {{ task.can_continue ? t('{user} 分享给你的对话，可以继续追问（在对方电脑上的同一会话里执行）', { user: task.shared_by }) : t('{user} 分享给你的对话，只读', { user: task.shared_by }) }}
      </n-alert>
      <n-alert v-if="task?.source === 'local' && !task.shared_by" type="default" :bordered="false" class="chat-alert">
        {{ t('从本机会话导入，继续对话会在原会话里接着执行。') }}
      </n-alert>
      <n-alert v-if="task?.status === 'pending_approval'" type="warning" :bordered="false" class="chat-alert">
        {{ task.can_approve ? t('有人请求在你的电脑上执行，批准后智能体按其隔离级别在授权工作区内运行。') : t('等待 {owner} 审批，批准后自动开始。', { owner: task.owner_username }) }}
      </n-alert>
      <p v-if="task && task.sees_process === false" class="hint">{{ t('执行过程已脱敏：密码、令牌等会打码，读写密钥或凭据的操作整条隐藏，本机路径只显示最后一级；完整路径只有归属人 {owner} 可见。', { owner: task.owner_username }) }}</p>
      <section v-for="turn in turns" :key="turn.turn" class="chat-turn">
        <div v-if="turn.prompt" class="bubble-row me">
          <div v-if="turn.turn === 1 && task?.request" class="bubble me">
            <span class="sender">{{ t('群聊「{name}」', { name: task.request.group_name }) }}</span>
            <pre>{{ task.request.content }}</pre>
            <details class="full-prompt"><summary>{{ t('查看发给智能体的完整内容（含群聊上下文）') }}</summary><pre>{{ turn.prompt.content }}</pre></details>
            <small>{{ formatTime(turn.prompt.created_at) }}</small>
          </div>
          <div v-else class="bubble me">
            <span v-if="turn.prompt.payload?.by" class="sender">{{ turn.prompt.payload.by }}</span>
            <pre>{{ turn.prompt.content }}</pre>
            <TaskFiles :task-id="taskId" :files="turn.inputs" />
            <small>{{ formatTime(turn.prompt.created_at) }}</small>
          </div>
        </div>
        <div class="bubble-row agent">
          <span class="avatar"><Bot :size="16" /></span>
          <div class="bubble agent">
            <button v-if="turn.output.length" class="output-toggle" @click="expanded[turn.turn] = !showOutput(turn)">
              <component :is="showOutput(turn) ? ChevronDown : ChevronRight" :size="14" />
              {{ t('执行过程 {n} 条', { n: turn.output.length }) }}{{ isLive(turn) ? t('（实时）') : '' }}
            </button>
            <div v-if="turn.output.length && showOutput(turn)" class="output-log">
              <template v-for="e in turn.output" :key="e.id">
                <div v-if="e.kind === 'tool' && e.payload" :class="['tool-line', 'phase-' + e.payload.phase]">
                  <span class="tool-line__mark" aria-hidden="true">{{ e.payload.phase === 'started' ? '▸' : e.payload.phase === 'failed' ? '✕' : '✓' }}</span>
                  <strong>{{ e.payload.name }}</strong>
                  <span class="tool-line__summary">{{ e.payload.summary }}</span>
                  <small v-if="e.payload.exit_code !== undefined">exit {{ e.payload.exit_code }}</small>
                  <router-link class="tool-line__more" :to="{ name: 'task-detail', params: { id: taskId }, query: { view: 'flow' } }" :aria-label="t('在执行流查看 {name} 的详情', { name: e.payload.name })">{{ t('详情') }}</router-link>
                </div>
                <pre v-else>{{ e.content }}</pre>
              </template>
            </div>
            <MarkdownText v-if="turn.result" class="reply" :text="turn.result.content" />
            <MarkdownText v-else-if="draftOf(turn)" class="reply reply--draft" :text="draftOf(turn)" />
            <pre v-if="turn.error" class="reply error">{{ turn.error.content }}</pre>
            <TaskFiles :task-id="taskId" :files="turn.files" />
            <div v-if="turn.notes.length" class="turn-notes">
              <span v-for="n in turn.notes" :key="n.id" :class="['turn-note', 'note-' + n.kind]">{{ n.kind === 'usage' ? t('用量') + ' · ' : '' }}{{ n.content }}</span>
            </div>
            <div v-if="!turn.result && !turn.error" class="typing">
              <template v-if="isLive(turn)"><n-spin :size="14" /> {{ liveNote(turn) }}</template>
              <template v-else>{{ turn.status.at(-1)?.content || '—' }}</template>
            </div>
            <small v-if="turn.result || turn.error">{{ formatTime((turn.result || turn.error).created_at) }}</small>
          </div>
        </div>
      </section>
    </div>

    <div class="chat-input">
      <ImageAttach v-model="images" :disabled="!canSend">
        <n-input
          v-model:value="draft"
          type="textarea"
          :autosize="{ minRows: 2, maxRows: 8 }"
          :disabled="!canSend"
          :placeholder="task?.can_continue === false ? t('只读分享，不能继续对话') : canSend ? t('继续对话（同一会话、同一工作区，Ctrl/⌘+Enter 发送）') : t('智能体回复后可继续对话')"
          @keydown.ctrl.enter="send"
          @keydown.meta.enter="send"
        />
      </ImageAttach>
      <n-button type="primary" :loading="busy" :disabled="!canSend || (!draft.trim() && !images.length)" @click="send">
        <template #icon><Send /></template>{{ t('发送') }}
      </n-button>
    </div>
    <ShareDialog v-model:show="showShare" :task="task" />
  </div>
</template>
