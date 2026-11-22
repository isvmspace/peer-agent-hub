<script setup>
import { ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import { useRouter } from 'vue-router'
import { api } from '../api/client'
import { formatTime } from '../utils/format'
import { t } from '../i18n'

// The owner's own Claude Code / Codex conversations on that computer, importable as hub conversations.
const props = defineProps({ show: Boolean, agent: { type: Object, default: null } })
const emit = defineEmits(['update:show'])
const message = useMessage()
const router = useRouter()
const sessions = ref([])
const loading = ref(false)
const importing = ref('')

let seq = 0
async function load() {
  // a slow answer for an agent the dialog has since moved off must not fill its list
  const mine = ++seq
  const agentId = props.agent.id
  loading.value = true
  sessions.value = []
  try {
    const { sessions: rows } = await api(`/api/agents/${agentId}/local-sessions`)
    if (mine === seq) sessions.value = rows
  } catch (e) { if (mine === seq) message.error(e.message) } finally { if (mine === seq) loading.value = false }
}

async function open(s) {
  if (s.task_id) return go(s.task_id)
  importing.value = s.id
  try {
    const { task } = await api(`/api/agents/${props.agent.id}/local-sessions`, { method: 'POST', body: { session_id: s.id } })
    message.success(t('已导入，可以在对话页点「分享」'))
    go(task.id)
  } catch (e) { message.error(e.message) } finally { importing.value = '' }
}

function go(id) {
  emit('update:show', false)
  router.push({ name: 'task-detail', params: { id } })
}

watch(() => [props.show, props.agent?.id], ([v]) => { if (v) load(); else seq++ })
</script>

<template>
  <n-modal :show="show" preset="card" :title="t('本机会话 · {address}', { address: agent?.address })" class="form-modal wide" @update:show="v => emit('update:show', v)">
    <p class="hint">{{ t('只列出在该智能体授权工作区里开始的最近会话（从 {host} 实时读取）。导入后只有你能看到，需要再点「分享」给同事；同事追问时会在你电脑上恢复这个会话继续。没看到想要的会话，先把它所在的项目目录加为工作区。', { host: agent?.hostname }) }}</p>
    <n-spin :show="loading">
      <div v-if="!loading && !sessions.length" class="empty-hint">{{ t('没有找到工作区内的会话') }}</div>
      <div v-for="s in sessions" :key="s.id" class="session-row">
        <div class="secondary-cell"><strong>{{ s.title }}</strong><small>{{ s.cwd }} · {{ formatTime(s.updated_at) }}</small></div>
        <n-button size="small" :type="s.task_id ? 'default' : 'primary'" secondary :loading="importing === s.id" @click="open(s)">
          {{ s.task_id ? t('打开') : t('导入') }}
        </n-button>
      </div>
    </n-spin>
  </n-modal>
</template>
