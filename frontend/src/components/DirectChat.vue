<script setup>
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import { Send } from '@lucide/vue'
import { api } from '../api/client'
import { openStream } from '../api/stream'
import { useAuth } from '../composables/useAuth'
import { formatTime } from '../utils/format'
import { useDraft } from '../composables/useDraft'
import { t } from '../i18n'

// Person-to-person chat with a friend; new messages arrive over SSE.
const props = defineProps({ friend: { type: Object, required: true } })
const emit = defineEmits(['read'])
const message = useMessage()
const { state: auth } = useAuth()
const messages = ref([])
const { draft, clear } = useDraft(() => `dm:${props.friend.user_id}`)
const busy = ref(false)
const scroller = ref(null)
const older = ref(false) // whether messages before the loaded ones may exist
const loadingOlder = ref(false)
const MAX_CHARS = 10000
let lastId = 0
let first = true
let close = null

async function scrollDown() {
  await nextTick()
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight
}

function connect() {
  close?.()
  messages.value = []
  lastId = 0
  first = true
  older.value = false
  close = openStream(
    () => `/api/dm/${props.friend.user_id}/stream?after=${lastId}`,
    (name, data) => {
      if (name !== 'messages' || !data.messages.length) return
      if (first) older.value = data.messages.length >= 300 // the first load is capped at the latest 300
      first = false
      messages.value.push(...data.messages)
      lastId = data.messages[data.messages.length - 1].id
      emit('read')
      scrollDown()
    },
    error => message.error(error.message),
  )
}

async function loadOlder() {
  if (loadingOlder.value || !messages.value.length) return
  const to = props.friend.user_id
  loadingOlder.value = true
  try {
    const data = await api(`/api/dm/${to}?before=${messages.value[0].id}`)
    if (to !== props.friend.user_id) return
    const el = scroller.value
    const from = el ? el.scrollHeight - el.scrollTop : 0
    messages.value.unshift(...data.messages)
    older.value = data.messages.length >= 100
    await nextTick()
    if (el) el.scrollTop = el.scrollHeight - from // keep the reading position
  } catch (e) {
    message.error(e.message)
  } finally {
    loadingOlder.value = false
  }
}

async function send() {
  if (busy.value || !draft.value.trim()) return
  const to = props.friend.user_id
  busy.value = true
  try {
    const sent = draft.value
    await api(`/api/dm/${to}`, { method: 'POST', body: { content: sent } })
    clear(`dm:${to}`, sent)
  } catch (e) {
    message.error(e.message)
  } finally {
    busy.value = false
  }
}

watch(() => props.friend.user_id, connect, { immediate: true })
onBeforeUnmount(() => close?.())
</script>

<template>
  <div class="chat-thread">
    <div class="chat-thread__bar">
      <div class="chat-thread__title">
        <strong>{{ friend.display_name || friend.username }}</strong>
        <small>@{{ friend.username }}</small>
      </div>
      <slot name="actions" />
    </div>
    <div ref="scroller" class="chat-thread__body">
      <div v-if="older" class="chat-older">
        <n-button size="small" quaternary :loading="loadingOlder" @click="loadOlder">{{ t('加载更早的消息') }}</n-button>
      </div>
      <div v-if="!messages.length" class="empty-hint">{{ t('和 {name} 打个招呼吧', { name: friend.display_name || friend.username }) }}</div>
      <div v-for="m in messages" :key="m.id" :class="['bubble-row', m.from_user_id === auth.user?.id ? 'me' : 'agent']">
        <span v-if="m.from_user_id !== auth.user?.id" class="avatar person">{{ (m.display_name || m.username).slice(0, 1) }}</span>
        <div :class="['bubble', m.from_user_id === auth.user?.id ? 'me' : 'agent']">
          <span v-if="m.auto" class="bubble__auto">🤖 {{ t('{agent} 自动回复', { agent: m.auto_agent }) }}</span>
          <pre>{{ m.content }}</pre><small>{{ formatTime(m.created_at) }}</small>
        </div>
      </div>
    </div>
    <div class="chat-input">
      <n-input
        v-model:value="draft"
        type="textarea"
        :autosize="{ minRows: 2, maxRows: 8 }"
        :placeholder="t('发消息（Ctrl/⌘+Enter 发送）')"
        :maxlength="MAX_CHARS"
        show-count
        @keydown.ctrl.enter="send"
        @keydown.meta.enter="send"
      />
      <n-button type="primary" :loading="busy" :disabled="!draft.trim()" @click="send"><template #icon><Send /></template>{{ t('发送') }}</n-button>
    </div>
  </div>
</template>
