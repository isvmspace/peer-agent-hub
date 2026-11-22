<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import { CircleAlert, Info, Send } from '@lucide/vue'
import { api } from '../api/client'
import { useAuth } from '../composables/useAuth'
import { ISOLATION } from '../utils/format'
import { t } from '../i18n'
const TASK_PLACEHOLDER = t('对方智能体看不到你的上下文，请写清楚目标、代码位置、步骤和期望产出。') + '\n' + t('例如：在 D:\\code\\desktop-client 下拉取 master 最新代码，执行 npm run build:win 打包安装包并安装，完成后告诉我安装包路径和版本号。')

const { state: auth } = useAuth()

const props = defineProps({
  show: { type: Boolean, default: false },
  agent: { type: String, default: '' },
})
const emit = defineEmits(['update:show', 'created'])
const router = useRouter()
const message = useMessage()
const agents = ref([])
const busy = ref(false)
const form = reactive({ agent: '', title: '', prompt: '', workspace: '' })

const options = computed(() =>
  agents.value.map(a => ({
    label: `${a.owner_name} / ${a.name}（${a.address}）${a.online ? '' : ' · ' + t('离线')}`,
    value: a.address,
  })),
)
const workspaceOptions = computed(() => {
  const agent = agents.value.find(a => a.address === form.agent)
  if (!agent) return []
  const own = agent.user_id === auth.user?.id
  const items = (agent.workspaces || []).map(w => ({ label: own ? `${w.name} · ${w.path}` : w.name, value: w.id }))
  return own ? [{ label: t('默认目录'), value: '' }, ...items] : items
})
watch(workspaceOptions, options => { form.workspace = options[0]?.value ?? '' })

// What would happen if this were sent now. Advisory only: the server checks again on submit.
const check = ref(null)
let checkSeq = 0
async function precheck() {
  const mine = ++checkSeq
  if (!form.agent) { check.value = null; return }
  try {
    const result = await api('/api/tasks/precheck', { method: 'POST', body: { agent: form.agent, workspace: form.workspace } })
    if (mine === checkSeq) check.value = result
  } catch (error) {
    if (mine === checkSeq) check.value = { ok: false, problems: [error.message], notes: [] }
  }
}
watch(() => [form.agent, form.workspace, props.show], () => { if (props.show) precheck() })
const busyAgent = computed(() => check.value?.online && (check.value.load?.running || check.value.load?.queued))
const headline = computed(() => {
  const c = check.value
  if (!c?.ok) return t('现在不能派发')
  if (c.needs_approval) return t('可以派发，需对方审批后执行')
  if (!c.online) return t('可以派发，智能体上线后执行')
  return busyAgent.value ? t('可以派发，需要排队') : t('可以派发，立即执行')
})
// the load is already in the headline row; don't repeat it as a note
const notes = computed(() => (check.value?.notes || []).filter(n => !n.startsWith('当前运行')))
const loadText = computed(() => {
  const l = check.value?.load
  if (!check.value?.online || !l) return ''
  return t('运行 {running}/1 · 排队 {queued}', { running: l.running, queued: l.queued }) + (l.queued && l.oldest_wait_s ? ' · ' + t('最长等待 {s} 秒', { s: l.oldest_wait_s }) : '')
})

watch(
  () => props.show,
  async visible => {
    if (!visible) return
    form.agent = props.agent || form.agent
    try {
      agents.value = (await api('/api/agents')).agents
    } catch (error) {
      message.error(error.message)
    }
  },
)

async function submit() {
  if (!form.agent || !form.prompt.trim()) {
    message.warning(t('请选择智能体并填写任务内容'))
    return
  }
  busy.value = true
  try {
    const { task } = await api('/api/tasks', { method: 'POST', body: { ...form } })
    message.success(task.status === 'pending_approval' ? t('已提交，等待对方审批') : t('任务已下发'))
    emit('update:show', false)
    emit('created', task)
    form.title = ''
    form.prompt = ''
    router.push({ name: 'task-detail', params: { id: task.id } })
  } catch (error) {
    message.error(error.message)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <n-modal :show="show" preset="card" :title="t('派发任务')" class="form-modal" @update:show="v => emit('update:show', v)">
    <n-form label-placement="top">
      <n-form-item :label="t('目标智能体')">
        <n-select v-model:value="form.agent" filterable :options="options" :placeholder="t('选择要派给谁')" />
      </n-form-item>
      <n-form-item :label="t('工作区')">
        <n-select v-model:value="form.workspace" :options="workspaceOptions" :disabled="!form.agent" />
      </n-form-item>
      <div v-if="check" class="precheck" :class="{ blocked: !check.ok }" aria-live="polite">
        <div class="precheck__head">
          <span :class="['dot', check.online ? 'on' : 'off']" />
          <strong>{{ headline }}</strong>
          <span v-if="loadText" class="precheck__load">{{ loadText }}</span>
          <n-tag v-if="check.agent" size="tiny" :bordered="false" :type="ISOLATION[check.agent.isolation]?.type">{{ ISOLATION[check.agent.isolation]?.label }}</n-tag>
        </div>
        <p v-for="p in check.problems" :key="p" class="precheck__problem"><CircleAlert :size="14" />{{ p }}</p>
        <p v-for="n in notes" :key="n" class="precheck__note"><Info :size="14" />{{ n }}</p>
      </div>
      <n-form-item :label="t('标题（可选）')">
        <n-input v-model:value="form.title" :placeholder="t('默认取任务内容第一行')" />
      </n-form-item>
      <n-form-item :label="t('任务内容')">
        <n-input
          v-model:value="form.prompt"
          type="textarea"
          :autosize="{ minRows: 6, maxRows: 16 }"
          :placeholder="TASK_PLACEHOLDER"
        />
      </n-form-item>
    </n-form>
    <template #footer>
      <n-space justify="end">
        <n-button @click="emit('update:show', false)">{{ t('取消') }}</n-button>
        <n-button type="primary" :loading="busy" :disabled="check && !check.ok" @click="submit">
          <template #icon><Send /></template>
          {{ t('派发') }}
        </n-button>
      </n-space>
    </template>
  </n-modal>
</template>
