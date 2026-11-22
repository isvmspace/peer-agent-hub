<script setup>
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, GitCommitHorizontal } from '@lucide/vue'
import ChatThread from '../components/ChatThread.vue'
import ExecutionFlow from '../components/ExecutionFlow.vue'
import ArtifactsPanel from '../components/ArtifactsPanel.vue'
import StatusTag from '../components/StatusTag.vue'
import { api } from '../api/client'
import { useAuth } from '../composables/useAuth'
import { duration, formatTime } from '../utils/format'
import { t } from '../i18n'

const emit = defineEmits(['changed'])
const route = useRoute()
const router = useRouter()
const { state: auth } = useAuth()
const task = ref(null)
const children = ref([])
const taskId = computed(() => Number(route.params.id))
const tab = computed({
  get: () => (route.query.view === 'flow' ? 'flow' : 'chat'),
  set: v => router.replace({ query: { ...route.query, view: v === 'flow' ? 'flow' : undefined } }),
})
const party = computed(() => task.value && [task.value.from_user_id, task.value.owner_user_id].includes(auth.user?.id))

async function onLoaded(t) {
  task.value = t
  emit('changed')
  try { children.value = (await api(`/api/tasks/${t.id}?after=999999999`)).children } catch { /* shown by the thread */ }
}
function onUpdate(t) {
  if (t.id === taskId.value) task.value = t
}
</script>

<template>
  <div class="page-wrap">
    <n-button text class="back-link" @click="router.back()"><template #icon><ArrowLeft /></template>{{ t('返回') }}</n-button>
    <div class="task-detail-grid">
      <n-card class="chat-card" content-style="padding:0">
        <n-tabs v-model:value="tab" type="line" class="detail-tabs" :tabs-padding="16">
          <n-tab-pane name="chat" :tab="t('对话')" display-directive="show">
            <ChatThread :task-id="taskId" @loaded="onLoaded" @update="onUpdate" @changed="emit('changed')" />
          </n-tab-pane>
          <n-tab-pane name="flow" :tab="t('执行流')">
            <ExecutionFlow :task-id="taskId" :can-cancel="!!party" />
          </n-tab-pane>
        </n-tabs>
      </n-card>
      <div v-if="task" class="detail-side">
        <n-card :title="t('任务信息')" size="small">
          <p v-if="task.wait?.text" class="wait-banner" :class="'wait-' + task.wait.code">{{ task.wait.text }}</p>
          <n-descriptions :column="1" label-placement="left" size="small">
            <n-descriptions-item :label="t('状态')"><StatusTag :status="task.status" /></n-descriptions-item>
            <n-descriptions-item :label="t('发起人')">{{ task.from_username }}{{ task.from_agent ? '/' + task.from_agent : '' }}</n-descriptions-item>
            <n-descriptions-item :label="t('执行智能体')">{{ task.agent_address }}</n-descriptions-item>
            <n-descriptions-item :label="t('工作区')">{{ task.workspace || t('默认目录') }}</n-descriptions-item>
            <n-descriptions-item :label="t('审批')">{{ task.auto_approved ? t('免审批') : task.decided_by ? t('人工审批') : '—' }}</n-descriptions-item>
            <n-descriptions-item :label="t('轮次')">{{ task.turn }}</n-descriptions-item>
            <n-descriptions-item v-if="task.attempts > task.turn" :label="t('重新下发')">{{ t('{n} 次（连接器断开后重发，共下发 {attempts} 次 / {turns} 轮）', { n: task.attempts - task.turn, attempts: task.attempts, turns: task.turn }) }}</n-descriptions-item>
            <n-descriptions-item :label="t('委派层级')">{{ task.depth }}</n-descriptions-item>
            <n-descriptions-item :label="t('会话 ID')">{{ task.session_id || '—' }}</n-descriptions-item>
            <n-descriptions-item v-if="task.code_ref?.commit" :label="t('代码基线')">
              <span class="code-ref"><GitCommitHorizontal :size="14" /><code :title="task.code_ref.commit">{{ task.code_ref.commit.slice(0, 8) }}</code>{{ task.code_ref.branch ? ' ' + task.code_ref.branch : '' }}{{ task.code_ref.dirty === 'true' ? t('（有未提交改动）') : '' }}</span>
            </n-descriptions-item>
            <n-descriptions-item :label="t('创建')">{{ formatTime(task.created_at) }}</n-descriptions-item>
            <n-descriptions-item :label="t('耗时')">{{ duration(task.created_at, task.finished_at) }}</n-descriptions-item>
          </n-descriptions>
          <p v-if="task.parent_task_id" class="hint">{{ t('父任务') }} <a href="javascript:void 0" @click="router.push({ name: 'task-detail', params: { id: task.parent_task_id } })">#{{ task.parent_task_id }}</a></p>
          <template v-if="children.length">
            <n-divider title-placement="left">{{ t('子任务') }}</n-divider>
            <div v-for="c in children" :key="c.id" class="child-task" @click="router.push({ name: 'task-detail', params: { id: c.id } })">
              <span>#{{ c.id }} {{ c.title }}</span><StatusTag :status="c.status" />
            </div>
            <n-button text type="primary" size="small" @click="tab = 'flow'">{{ t('在执行流里查看整棵任务树') }}</n-button>
          </template>
        </n-card>
        <n-card :title="t('产物')" size="small">
          <ArtifactsPanel :task="task" :me="auth.user?.id || 0" @changed="emit('changed')" />
        </n-card>
      </div>
    </div>
  </div>
</template>
