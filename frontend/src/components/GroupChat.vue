<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import { Bot, FolderOpen, Send } from '@lucide/vue'
import StatusTag from './StatusTag.vue'
import TaskFiles from './TaskFiles.vue'
import { api } from '../api/client'
import { openStream } from '../api/stream'
import { useAuth } from '../composables/useAuth'
import { ACTIVE_STATUSES, formatTime } from '../utils/format'
import { useDraft } from '../composables/useDraft'

import MarkdownText from './MarkdownText.vue'
import { t } from '../i18n'
const props = defineProps({ groupId: { type: Number, required: true } })
const emit = defineEmits(['open-task'])
const message = useMessage()
const { state: auth } = useAuth()
const members = ref([])
const people = ref([])
const timeline = ref([])
const older = ref(false) // whether messages before the shown ones may exist
const loadingOlder = ref(false)
let since = 0 // oldest message the live stream covers; loading older moves it back
const { draft, clear } = useDraft(() => `group:${props.groupId}`)
const busy = ref(false)
const scroller = ref(null)
let close = null

// @ picker: agents by address (they get a task), people by username (just a highlight)
const mentionOptions = computed(() => [
  ...(members.value.length > 1 ? [{ label: t('所有智能体'), value: 'all' }] : []),
  ...members.value.map(m => ({ label: `${m.name}（${m.address}）`, value: m.address })),
  ...people.value.filter(p => p.id !== auth.user?.id).map(p => ({ label: `${p.display_name || p.username}（${p.username}）`, value: p.username })),
])
// one time-ordered stream: replies land when they finish; unfinished ones stay at the bottom like "typing"
const items = computed(() => {
  const list = []
  for (const m of timeline.value) {
    list.push({ key: `m${m.id}`, at: m.created_at, message: m })
    for (const r of m.replies) list.push({ key: `r${r.id}`, at: r.finished_at || Infinity, reply: r, to: m })
  }
  return list.sort((a, b) => a.at - b.at || a.key.localeCompare(b.key))
})
const soloWithAgents = computed(() => people.value.length <= 1)

async function scrollDown(force = false) {
  await nextTick()
  const el = scroller.value
  if (el && (force || el.scrollHeight - el.scrollTop - el.clientHeight < 240)) el.scrollTop = el.scrollHeight
}

function connect() {
  since = 0
  older.value = false
  timeline.value = []
  stream()
}

function stream() {
  close?.()
  close = openStream(
    () => `/api/groups/${props.groupId}/stream?since=${since}`,
    (name, data) => {
      if (name !== 'timeline') return
      const first = !timeline.value.length
      if (!since && data.timeline.length >= 200) older.value = true // the first load carries the latest 200
      since = data.since || since
      members.value = data.members
      people.value = data.people || []
      timeline.value = data.timeline
      scrollDown(first)
    },
    error => message.error(error.message),
  )
}

async function loadOlder() {
  if (loadingOlder.value || !timeline.value.length) return
  const groupId = props.groupId
  loadingOlder.value = true
  try {
    const data = await api(`/api/groups/${groupId}?before=${timeline.value[0].id}`)
    if (groupId !== props.groupId || !data.timeline.length) {
      older.value = false
      return
    }
    const el = scroller.value
    const from = el ? el.scrollHeight - el.scrollTop : 0
    timeline.value = [...data.timeline, ...timeline.value]
    older.value = data.timeline.length >= 100
    since = data.timeline[0].id
    stream() // the live stream now keeps the older page up to date too
    await nextTick()
    if (el) el.scrollTop = el.scrollHeight - from // keep the reading position
  } catch (e) {
    message.error(e.message)
  } finally {
    loadingOlder.value = false
  }
}

function highlight(text) {
  const esc = text.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c])
  return esc.replace(/@([\w.\-/一-鿿]+)/g, '<span class="mention">@$1</span>')
}

async function send() {
  if (busy.value || !draft.value.trim()) return
  const groupId = props.groupId
  busy.value = true
  try {
    const sent = draft.value
    const result = await api(`/api/groups/${groupId}/messages`, { method: 'POST', body: { content: sent } })
    for (const err of result.errors) message.warning(err)
    clear(`group:${groupId}`, sent)
    scrollDown(true)
  } catch (e) {
    message.error(e.message)
  } finally {
    busy.value = false
  }
}

async function decide(reply, action) {
  try { await api(`/api/tasks/${reply.id}/${action}`, { method: 'POST', body: {} }) } catch (e) { message.error(e.message) }
}

function onKeydown(e) {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) send()
}

watch(() => props.groupId, connect, { immediate: true })
onBeforeUnmount(() => close?.())
</script>

<template>
  <div class="chat-thread">
    <div class="chat-thread__bar">
      <div class="chat-thread__title">
        <strong><slot name="title" /></strong>
        <small>
          <span v-for="p in people" :key="'u' + p.id" class="member-chip">{{ p.display_name || p.username }}<template v-if="p.is_owner">{{ t('（群主）') }}</template></span>
          <span v-for="m in members" :key="m.id" class="member-chip"><span :class="['dot', m.online ? 'on' : 'off']" /><Bot :size="11" /> {{ m.address }}
            <template v-if="m.group_workspace"> · {{ m.group_workspace }}</template></span>
        </small>
      </div>
      <slot name="actions" />
    </div>
    <div ref="scroller" class="chat-thread__body">
      <div v-if="older" class="chat-older">
        <n-button size="small" quaternary :loading="loadingOlder" @click="loadOlder">{{ t('加载更早的消息') }}</n-button>
      </div>
      <div v-if="!timeline.length" class="empty-hint">
        {{ soloWithAgents ? t('发一条消息，群里的智能体会同时开始工作并各自回复') : t('和群友聊天；@ 某个智能体（或 @所有智能体）才会让它干活') }}
      </div>
      <template v-for="item in items" :key="item.key">
        <div v-if="item.message" :class="['bubble-row', item.message.user_id === auth.user?.id ? 'me' : 'agent']">
          <span v-if="item.message.user_id !== auth.user?.id" class="avatar person">{{ (item.message.display_name || item.message.username).slice(0, 1) }}</span>
          <div :class="['bubble', item.message.user_id === auth.user?.id ? 'me' : 'agent']">
            <span class="sender">{{ item.message.display_name || item.message.username }}</span>
            <pre v-html="highlight(item.message.content)" />
            <small>{{ formatTime(item.message.created_at) }}</small>
          </div>
        </div>
        <div v-else class="bubble-row agent">
          <span class="avatar"><Bot :size="16" /></span>
          <div class="bubble agent clickable" :title="t('打开完整对话')" @click="emit('open-task', item.reply.id)">
            <span class="sender">{{ item.reply.agent_name }} <small>{{ item.reply.agent_address }}</small> <StatusTag :status="item.reply.status" /></span>
            <div class="reply-quote">{{ t('回复 {name}：', { name: item.to.display_name || item.to.username }) }}{{ item.to.content }}</div>
            <span v-if="item.reply.workspace" class="ws-chip"><FolderOpen :size="12" />{{ item.reply.workspace }}</span>
            <MarkdownText v-if="item.reply.result" class="reply" :text="item.reply.result" />
            <pre v-else-if="item.reply.error" class="reply error">{{ item.reply.error }}</pre>
            <div v-else-if="ACTIVE_STATUSES.includes(item.reply.status)" class="typing">
              <n-spin :size="14" /> <span class="live-line">{{ item.reply.live || t('处理中…') }}</span>
            </div>
            <div v-if="item.reply.status === 'pending_approval'" class="inline-approve" @click.stop>
              <template v-if="item.reply.owner_user_id === auth.user?.id">
                <span>{{ t('请求在你的电脑上执行') }}</span>
                <n-button size="tiny" type="primary" @click="decide(item.reply, 'approve')">{{ t('批准') }}</n-button>
                <n-button size="tiny" @click="decide(item.reply, 'reject')">{{ t('拒绝') }}</n-button>
              </template>
              <span v-else>{{ t('等待 {owner} 审批', { owner: item.reply.owner_username }) }}</span>
            </div>
            <div @click.stop><TaskFiles :task-id="item.reply.id" :files="item.reply.attachments || []" /></div>
            <small v-if="item.reply.finished_at">{{ formatTime(item.reply.finished_at) }}</small>
          </div>
        </div>
      </template>
    </div>
    <div class="chat-input">
      <div class="chat-input__main">
        <n-mention
          v-model:value="draft"
          type="textarea"
          :options="mentionOptions"
          :autosize="{ minRows: 2, maxRows: 8 }"
          :placeholder="soloWithAgents ? t('发给群里所有智能体，输入 @ 可只点名部分（Ctrl/⌘+Enter 发送）') : t('输入 @ 点名智能体或群友，被 @ 的智能体才会干活（Ctrl/⌘+Enter 发送）')"
          @keydown="onKeydown"
        />
      </div>
      <n-button type="primary" :loading="busy" :disabled="!draft.trim()" @click="send"><template #icon><Send /></template>{{ t('发送') }}</n-button>
    </div>
  </div>
</template>
