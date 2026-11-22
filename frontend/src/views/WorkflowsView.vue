<script setup>
// Fixed multi-agent templates. The hub starts each step when the steps it depends on
// completed and hands it their results and artifact numbers; a gated step waits for
// the requester. The run view lays steps out by dependency level and refreshes while
// the run is in progress.
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useDialog, useMessage } from 'naive-ui'
import { Ban, CirclePlay, GitBranch, Play, Workflow } from '@lucide/vue'
import PageHeader from '../components/PageHeader.vue'
import StatusTag from '../components/StatusTag.vue'
import { api } from '../api/client'
import { useAuth } from '../composables/useAuth'
import { t } from '../i18n'
import { formatTime } from '../utils/format'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const { state: auth } = useAuth()
const runs = ref([])
const templates = ref([])
const agents = ref([])
const run = ref(null)
const loading = ref(false)
const showNew = ref(false)
const busy = ref(false)
const form = reactive({ template: '', title: '', input: '', steps: [] })
const checks = ref({})

const STEP = {
  waiting: { label: t('等待上游'), type: 'default' },
  gated: { label: t('等你放行'), type: 'warning' },
  ready: { label: t('即将开始'), type: 'info' },
  starting: { label: t('启动中'), type: 'info' },
  running: { label: t('执行中'), type: 'primary' },
  completed: { label: t('已完成'), type: 'success' },
  failed: { label: t('失败'), type: 'error' },
  skipped: { label: t('已跳过'), type: 'default' },
  canceled: { label: t('已取消'), type: 'default' },
}
const RUN = { running: { label: t('进行中'), type: 'primary' }, completed: { label: t('已完成'), type: 'success' }, failed: { label: t('失败'), type: 'error' }, canceled: { label: t('已取消'), type: 'default' } }

const runId = computed(() => Number(route.query.run) || 0)
const spec = computed(() => templates.value.find(t => t.id === form.template))
const usable = computed(() => agents.value.filter(a => a.access === 'owner' || a.access === 'accepted'))
const agentOptions = computed(() => usable.value.map(a => ({ label: t('{owner} / {name}（{address}）', { owner: a.owner_name, name: a.name, address: a.address }) + (a.online ? '' : ' · ' + t('离线')), value: a.address })))

function workspaceOptions(address) {
  const agent = agents.value.find(a => a.address === address)
  if (!agent) return []
  const own = agent.user_id === auth.user?.id
  const items = (agent.workspaces || []).map(w => ({ label: own ? `${w.name} · ${w.path}` : w.name, value: w.id }))
  return own ? [{ label: t('默认目录'), value: '' }, ...items] : items
}

async function loadList() {
  loading.value = true
  try {
    const result = await api('/api/workflows')
    runs.value = result.runs
    templates.value = result.templates
  } catch (e) { message.error(e.message) } finally { loading.value = false }
}

let timer = null
let seq = 0
let disposed = false
const runError = ref('')
async function loadRun() {
  clearTimeout(timer)
  const mine = ++seq // every change of the selected run, including closing it, invalidates older responses
  if (!runId.value) { run.value = null; return }
  try {
    const result = await api(`/api/workflows/${runId.value}`)
    if (disposed || mine !== seq) return
    runError.value = ''
    run.value = result.run
    if (result.run.status === 'running') timer = setTimeout(loadRun, 2000)
    else loadList()
  } catch (e) {
    if (disposed || mine !== seq) return
    runError.value = e.message
    if (run.value?.status === 'running') timer = setTimeout(loadRun, 4000)
  }
}
watch(runId, loadRun, { immediate: true })
onBeforeUnmount(() => { disposed = true; seq++; clearTimeout(timer) })
loadList()

async function openNew() {
  showNew.value = true
  if (!agents.value.length) {
    try { agents.value = (await api('/api/agents')).agents } catch (e) { message.error(e.message) }
  }
  if (!form.template && templates.value.length) pickTemplate(templates.value[0].id)
}
watch(() => route.query.new, v => { if (v) { openNew(); router.replace({ query: { ...route.query, new: undefined } }) } }, { immediate: true })

function moveTemplate(e, index) {
  const n = templates.value.length
  const next = { ArrowRight: index + 1, ArrowDown: index + 1, ArrowLeft: index - 1, ArrowUp: index - 1, Home: 0, End: n - 1 }[e.key]
  if (next === undefined) return
  e.preventDefault()
  const target = templates.value[(next + n) % n]
  pickTemplate(target.id)
  e.currentTarget.parentElement.children[(next + n) % n]?.focus()
}

function pickTemplate(id) {
  form.template = id
  const t = templates.value.find(x => x.id === id)
  form.steps = (t?.steps || []).map((s, i) => ({ agent: form.steps[i]?.agent || '', workspace: '', gate: false }))
  checks.value = {}
}

const checkSeq = {}
async function precheck(i) {
  const step = form.steps[i]
  const mine = (checkSeq[i] = (checkSeq[i] || 0) + 1)
  if (!step?.agent) { checks.value = { ...checks.value, [i]: null }; return }
  const asked = `${form.template}|${step.agent}|${step.workspace}`
  checks.value = { ...checks.value, [i]: { checking: true } }
  let result
  try {
    result = await api('/api/tasks/precheck', { method: 'POST', body: { agent: step.agent, workspace: step.workspace } })
  } catch (e) {
    result = { ok: false, problems: [e.message], notes: [] }
  }
  // only the latest check for this step, and only if the step still asks the same thing
  const now = form.steps[i] && `${form.template}|${form.steps[i].agent}|${form.steps[i].workspace}`
  if (mine === checkSeq[i] && now === asked) checks.value = { ...checks.value, [i]: result }
}
function setAgent(i, value) {
  form.steps[i].agent = value
  form.steps[i].workspace = workspaceOptions(value)[0]?.value ?? ''
  precheck(i)
}
const ready = computed(() => form.input.trim() && form.steps.length && form.steps.every((s, i) => s.agent && checks.value[i]?.ok === true))

async function start() {
  busy.value = true
  try {
    const { run: created } = await api('/api/workflows', { method: 'POST', body: { template: form.template, title: form.title, input: form.input, steps: form.steps } })
    message.success(t('工作流已启动'))
    showNew.value = false
    form.input = ''
    form.title = ''
    await loadList()
    router.push({ query: { run: created.id } })
  } catch (e) { message.error(e.message) } finally { busy.value = false }
}

async function act(action, body = {}) {
  try {
    const result = await api(`/api/workflows/${run.value.id}/${action}`, { method: 'POST', body })
    run.value = result.run
    loadRun()
  } catch (e) { message.error(e.message) }
}
function cancelRun() {
  dialog.warning({
    title: t('取消工作流'),
    content: t('未开始的步骤不再执行，正在执行的步骤连同其子任务会被取消。'),
    positiveText: t('取消工作流'),
    negativeText: t('不取消'),
    onPositiveClick: () => act('cancel'),
  })
}

// steps grouped into columns by how many dependencies deep they are
const levels = computed(() => {
  const steps = run.value?.steps || []
  const depth = {}
  const level = s => (depth[s.idx] ??= s.depends_on.length ? 1 + Math.max(...s.depends_on.map(d => level(steps[d]))) : 0)
  const cols = []
  for (const s of steps) (cols[level(s)] ||= []).push(s)
  return cols
})
</script>

<template>
  <div class="page-wrap">
    <PageHeader :title="t('工作流')" :description="t('把实现、评审、测试交给不同的智能体，按依赖自动接力，每一步都能拿到上游的结果和产物')">
      <template #actions>
        <n-button type="primary" @click="openNew"><template #icon><Play /></template>{{ t('启动工作流') }}</n-button>
      </template>
    </PageHeader>

    <n-alert v-if="runError" type="warning" :bordered="false">
      {{ t('读取工作流 #{id} 失败：{error}', { id: runId, error: runError }) }}
      <n-button text type="primary" size="small" @click="loadRun">{{ t('重试') }}</n-button>
    </n-alert>
    <n-card v-if="run && run.id === runId" class="run-card">
      <template #header>
        <span class="run-title"><Workflow :size="18" />#{{ run.id }} {{ run.title }}</span>
      </template>
      <template #header-extra>
        <n-space align="center" size="small">
          <n-tag :type="RUN[run.status]?.type" :bordered="false">{{ RUN[run.status]?.label }}</n-tag>
          <n-button v-if="run.status === 'running'" size="small" type="error" secondary @click="cancelRun"><template #icon><Ban /></template>{{ t('取消') }}</n-button>
          <n-button size="small" quaternary @click="router.push({ query: {} })">{{ t('关闭详情') }}</n-button>
        </n-space>
      </template>
      <p class="hint">{{ run.template_name }} · {{ t('{time} 启动', { time: formatTime(run.created_at) }) }}</p>
      <div class="dag" role="list" :aria-label="t('工作流步骤')">
        <div v-for="(col, ci) in levels" :key="ci" class="dag-col">
          <article v-for="s in col" :key="s.idx" role="listitem" :class="['dag-step', 'step-' + s.status]">
            <header>
              <span class="dag-step__name">{{ s.name }}</span>
              <n-tag size="small" :bordered="false" :type="STEP[s.status]?.type">{{ STEP[s.status]?.label || s.status }}</n-tag>
            </header>
            <small class="addr">{{ s.agent_address }}</small>
            <small v-if="s.depends_on.length" class="dag-step__deps"><GitBranch :size="12" />{{ t('等待 {steps}', { steps: s.depends_on.map(d => run.steps[d].name).join(t('、')) }) }}</small>
            <small v-if="s.wait?.text && s.status === 'running'" class="flow-wait">{{ s.wait.text }}</small>
            <div v-if="s.artifacts?.length" class="dag-step__artifacts">
              <span v-for="a in s.artifacts" :key="a.id" :class="['artifact-chip', 'review-' + a.review]">{{ a.ref }} {{ a.name }} v{{ a.version }}</span>
            </div>
            <footer>
              <n-button v-if="s.task_id" size="tiny" text type="primary" @click="router.push({ name: 'task-detail', params: { id: s.task_id }, query: { view: 'flow' } })">{{ t('任务 #{id}', { id: s.task_id }) }}</n-button>
              <n-button v-if="s.status === 'gated'" size="tiny" type="warning" @click="act('continue', { step: s.idx })"><template #icon><CirclePlay /></template>{{ t('放行') }}</n-button>
            </footer>
          </article>
        </div>
      </div>
      <details class="run-input"><summary>{{ t('需求内容') }}</summary><pre>{{ run.input }}</pre></details>
    </n-card>

    <n-card :title="t('我发起的工作流')">
      <n-empty v-if="!loading && !runs.length" :description="t('还没有工作流。点右上角「启动工作流」，选一个模板并给每一步指定智能体。')" />
      <div class="recent-list">
        <button v-for="r in runs" :key="r.id" :class="['recent-item', 'run-item', { selected: r.id === runId }]" @click="router.push({ query: { run: r.id } })">
          <span class="task-id">#{{ r.id }}</span>
          <span class="recent-item__body">
            <strong>{{ r.title }}</strong>
            <small>{{ r.template_name }}</small>
          </span>
          <span class="step-pills" aria-hidden="true"><i v-for="s in r.steps" :key="s.idx" :class="'pill-' + s.status" :title="`${s.name}：${STEP[s.status]?.label}`" /></span>
          <n-tag size="small" :type="RUN[r.status]?.type" :bordered="false">{{ RUN[r.status]?.label }}</n-tag>
          <small class="muted">{{ formatTime(r.created_at) }}</small>
        </button>
      </div>
    </n-card>

    <n-modal v-model:show="showNew" preset="card" :title="t('启动工作流')" class="form-modal wide">
      <div class="template-pick" role="radiogroup" :aria-label="t('选择模板')">
        <button
          v-for="(t, ti) in templates"
          :key="t.id"
          type="button"
          role="radio"
          :aria-checked="form.template === t.id"
          :tabindex="form.template === t.id ? 0 : -1"
          :class="['template-card', { active: form.template === t.id }]"
          @click="pickTemplate(t.id)"
          @keydown="e => moveTemplate(e, ti)"
        >
          <strong>{{ t.name }}</strong><small>{{ t.description }}</small>
        </button>
      </div>
      <n-form label-placement="top">
        <n-form-item :label="t('需求内容（每一步都会收到）')">
          <n-input v-model:value="form.input" type="textarea" :autosize="{ minRows: 4, maxRows: 12 }" :placeholder="t('写清目标、代码位置和期望产出；下游步骤会额外收到上游的结果和产物编号')" />
        </n-form-item>
        <n-form-item :label="t('标题（可选）')">
          <n-input v-model:value="form.title" :placeholder="t('默认取需求第一行')" />
        </n-form-item>
      </n-form>
      <div v-if="spec" class="step-assign">
        <div v-for="(s, i) in spec.steps" :key="i" class="step-assign__row">
          <span class="step-assign__name">{{ i + 1 }}. {{ s.name }}<small v-if="s.depends_on.length">{{ t('在「{steps}」之后', { steps: s.depends_on.map(d => spec.steps[d].name).join(t('、')) }) }}</small></span>
          <n-select :value="form.steps[i]?.agent || null" filterable :options="agentOptions" :placeholder="t('选择智能体')" @update:value="v => setAgent(i, v)" />
          <n-select v-model:value="form.steps[i].workspace" :options="workspaceOptions(form.steps[i]?.agent)" :disabled="!form.steps[i]?.agent" @update:value="() => precheck(i)" />
          <n-checkbox v-model:checked="form.steps[i].gate" :disabled="!s.depends_on.length">{{ t('开始前等我放行') }}</n-checkbox>
          <p v-if="checks[i]?.checking" class="step-assign__check">{{ t('检查中…') }}</p>
          <p v-else-if="checks[i]" :class="['step-assign__check', { bad: !checks[i].ok }]">
            {{ checks[i].ok ? [checks[i].online ? t('在线') : t('离线（会排队）'), checks[i].needs_approval ? t('需对方审批') : t('免审批'), ...checks[i].notes.filter(n => n.includes('排队'))].join(' · ') : checks[i].problems.join(t('；')) }}
          </p>
        </div>
      </div>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showNew = false">{{ t('取消') }}</n-button>
          <n-button type="primary" :disabled="!ready" :loading="busy" @click="start"><template #icon><Play /></template>{{ t('启动') }}</n-button>
        </n-space>
      </template>
    </n-modal>
  </div>
</template>
