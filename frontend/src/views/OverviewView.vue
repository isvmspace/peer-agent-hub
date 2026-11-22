<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Send } from '@lucide/vue'
import PageHeader from '../components/PageHeader.vue'
import StatusTag from '../components/StatusTag.vue'
import NewTaskModal from '../components/NewTaskModal.vue'
import AgentNetwork from '../components/AgentNetwork.vue'
import { openStream } from '../api/stream'
import { api } from '../api/client'
import { useAuth } from '../composables/useAuth'
import { formatTime } from '../utils/format'
import { t } from '../i18n'

const router = useRouter()
const { state: auth } = useAuth()
const data = ref(null)
const agents = ref([])
const showNew = ref(false)
const failed = ref(false)

// one live picture pushed by the hub whenever any task or agent changes (at most once a second)
let close = null
onMounted(() => {
  close = openStream('/api/overview/stream', (name, payload) => {
    if (name !== 'overview') return
    const { agents: list, ...rest } = payload
    data.value = rest
    agents.value = list
    failed.value = false
  }, () => { failed.value = true }) // keep the last good picture, but say it may be stale
})
onBeforeUnmount(() => close?.())

const openTask = id => router.push({ name: 'task-detail', params: { id } })
const openFlow = id => router.push({ name: 'task-detail', params: { id }, query: { view: 'flow' } })
// every active task plus the recent ones, so the network shows all work in flight
const scope = ref('all') // all | mine | active
const networkTasks = computed(() => {
  const seen = new Set()
  const me = auth.user?.username
  return [...(data.value?.active_tasks || []), ...(data.value?.recent_tasks || [])]
    .filter(t => !seen.has(t.id) && seen.add(t.id))
    .filter(t => scope.value !== 'mine' || t.from_username === me || t.turn_username === me || t.agent_address.startsWith(me + '/'))
    .filter(t => scope.value !== 'active' || !['completed', 'failed', 'rejected', 'canceled'].includes(t.status))
})
// a link carrying several tasks opens this list instead of guessing which one was meant
// keep only which tasks were picked; their status is read from each new snapshot so the list stays current
const picked = ref(null)
const pickedTasks = computed(() => {
  if (!picked.value) return []
  const all = [...(data.value?.active_tasks || []), ...(data.value?.recent_tasks || [])]
  return picked.value.ids.map(id => all.find(t => t.id === id) || picked.value.last.get(id))
})
// a picked task that has left the snapshot (finished long ago) is fetched on its own instead of showing its old state
watch(data, () => {
  if (!picked.value) return
  const all = [...(data.value?.active_tasks || []), ...(data.value?.recent_tasks || [])]
  for (const id of picked.value.ids) {
    if (all.some(t => t.id === id)) continue
    api(`/api/tasks/${id}`).then(r => { if (picked.value?.last.has(id)) picked.value.last.set(id, { ...picked.value.last.get(id), ...r.task }) }).catch(() => {})
  }
})
const pick = ({ title, tasks }) => { picked.value = { title, ids: tasks.map(t => t.id), last: reactive(new Map(tasks.map(t => [t.id, t]))) } }
const pickTask = id => { picked.value = null; openTask(id) }
</script>

<template>
  <div class="page-wrap">
    <PageHeader :title="t('协作概览')" :description="t('谁的智能体在线、任务正流向哪里')">
      <template #actions>
        <n-button type="primary" @click="showNew = true">
          <template #icon><Send /></template>{{ t('派发任务') }}
        </n-button>
      </template>
    </PageHeader>

    <n-alert v-if="failed" type="warning" :bordered="false">
      {{ data ? t('实时连接中断，下面是最后一次收到的数据，正在自动重连') : t('获取概览失败，正在自动重连') }}
    </n-alert>
    <section class="overview-hero">
      <div class="overview-net">
        <n-radio-group v-model:value="scope" size="small" class="net-scope">
          <n-radio-button value="all">{{ t('全部') }}</n-radio-button>
          <n-radio-button value="mine">{{ t('与我有关') }}</n-radio-button>
          <n-radio-button value="active">{{ t('只看进行中') }}</n-radio-button>
        </n-radio-group>
        <AgentNetwork :agents="agents" :tasks="networkTasks" :me="auth.user?.username" @open-task="openTask" @open-tasks="pick" />
      </div>
      <dl class="pulse">
        <div><dt>{{ t('在线智能体') }}</dt><dd><b>{{ data?.agents_online ?? '—' }}</b><span>/ {{ data?.agents_total ?? 0 }}</span></dd></div>
        <div><dt>{{ t('我的智能体') }}</dt><dd><b>{{ data?.my_agents ?? '—' }}</b></dd></div>
        <div class="pulse__action" :class="{ hot: data?.pending_approval }" @click="router.push({ name: 'tasks', query: { box: 'approval' } })">
          <dt>{{ t('待我审批') }}</dt><dd><b>{{ data?.pending_approval ?? '—' }}</b></dd>
        </div>
        <div><dt>{{ t('进行中') }}</dt><dd><b>{{ data?.running ?? '—' }}</b></dd></div>
      </dl>
    </section>

    <n-card v-if="data?.active_tasks?.length" class="recent-card">
      <template #header><span class="live-title"><span class="live-dot" />{{ t('进行中的任务 {n}', { n: data.active_tasks.length }) }}</span></template>
      <div class="recent-list">
        <button v-for="task in data.active_tasks" :key="task.id" class="recent-item active-item" @click="openFlow(task.id)">
          <span class="task-id">#{{ task.id }}</span>
          <span class="recent-item__body">
            <strong>{{ task.title }}</strong>
            <small><span class="addr">{{ task.from_username }}{{ task.from_agent ? '/' + task.from_agent : '' }}</span> {{ t('派给') }} <span class="addr">{{ task.agent_address }}</span></small>
            <small v-if="task.activity?.summary" class="activity-line">{{ task.activity.tool ? task.activity.tool + ' · ' : '' }}{{ task.activity.summary }}</small>
          </span>
          <span v-if="task.wait?.text" class="wait-chip" :class="'wait-' + task.wait.code">{{ task.wait.text }}</span>
          <StatusTag :status="task.status" />
        </button>
      </div>
    </n-card>

    <n-card :title="t('最近任务')" class="recent-card">
      <n-empty v-if="data && !data.recent_tasks.length" :description="t('还没有任务。点右上角「派发任务」，或在你的智能体里说“让 某人/claude 帮我…”')" />
      <div class="recent-list">
        <button v-for="task in data?.recent_tasks || []" :key="task.id" class="recent-item" @click="openTask(task.id)">
          <span class="task-id">#{{ task.id }}</span>
          <span class="recent-item__body">
            <strong>{{ task.title }}</strong>
            <small><span class="addr">{{ task.from_username }}{{ task.from_agent ? '/' + task.from_agent : '' }}</span> {{ t('派给') }} <span class="addr">{{ task.agent_address }}</span></small>
          </span>
          <StatusTag :status="task.status" />
          <small class="muted">{{ formatTime(task.updated_at) }}</small>
        </button>
      </div>
    </n-card>
    <NewTaskModal v-model:show="showNew" />
    <n-modal :show="!!picked" preset="card" :title="picked ? t('{title} · {n} 个任务', { title: picked.title, n: picked.ids.length }) : ''" style="width: min(560px, 92vw)" @update:show="v => !v && (picked = null)">
      <div class="recent-list">
        <button v-for="task in pickedTasks" :key="task.id" class="recent-item" @click="pickTask(task.id)">
          <span class="task-id">#{{ task.id }}</span>
          <span class="recent-item__body"><strong>{{ task.title }}</strong></span>
          <StatusTag :status="task.status" />
          <small class="muted">{{ formatTime(task.updated_at) }}</small>
        </button>
      </div>
    </n-modal>
  </div>
</template>
