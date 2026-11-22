<script setup>
// 「工作」：同事在工作服务器上自己的独立容器里，和智能体对话写代码，用自己的 GitLab 账号提交推送。
// 首次进入先建立连接（选服务器、GitLab 令牌、提交身份），之后是工作台：目录与仓库、会话、对话、改动与推送。
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useDialog, useMessage } from 'naive-ui'
import { useRoute, useRouter } from 'vue-router'
import { FolderGit2, GitBranch, KeyRound, Plus, RefreshCw, Send, Square, Trash2, Upload } from '@lucide/vue'
import PageHeader from '../components/PageHeader.vue'
import MarkdownText from '../components/MarkdownText.vue'
import ImageAttach from '../components/ImageAttach.vue'
import TaskFiles from '../components/TaskFiles.vue'
import { api } from '../api/client'
import { openStream } from '../api/stream'
import { useAuth } from '../composables/useAuth'
import { ACTIVE_STATUSES, STATUS, formatTime } from '../utils/format'
import { t } from '../i18n'

const message = useMessage()
const dialog = useDialog()
const route = useRoute()
const router = useRouter()
const { state: authState } = useAuth()
const loading = ref(true)
const home = ref({ servers: [], profile: null, dirs: [], sessions: [] })
const profile = computed(() => home.value.profile)

async function load() {
  try {
    home.value = await api('/api/work')
  } catch (e) {
    message.error(e.message)
  } finally {
    loading.value = false
  }
}

// ---- 首次连接 ----
const connect = ref({ connector_id: null, token: '', git_name: '', git_email: '', push_mode: 'confirm' })
const connecting = ref(false)
const serverOptions = computed(() => [
  ...(home.value.servers.length > 1 ? [{ label: t('自动选择（推荐：在线且人数最少的一台）'), value: 0 }] : []),
  ...home.value.servers.map(s => ({ label: `${s.name}${s.online ? '' : ' ' + t('（离线）')} · ${t('已开通 {n} 人', { n: s.users })}`, value: s.id, disabled: !s.online })),
])
watch(() => home.value, h => {
  if (connect.value.connector_id === null) connect.value.connector_id = h.servers.length > 1 ? 0 : h.servers.find(s => s.online)?.id ?? null
  const gl = h.gitlab_pending
  if (!connect.value.git_name) connect.value.git_name = gl?.name || authState.user?.display_name || authState.user?.username || ''
  if (!connect.value.git_email && gl?.email) connect.value.git_email = gl.email
})
async function gitlabAuthorize() {
  try {
    window.location.href = (await api('/api/work/gitlab/authorize', { method: 'POST' })).url
  } catch (e) {
    message.error(e.message)
  }
}
async function doConnect() {
  connecting.value = true
  try {
    await api('/api/work/connect', { method: 'POST', body: connect.value })
    message.success(t('已开通你的工作容器'))
    connect.value.token = ''
    await load()
  } catch (e) {
    message.error(e.message)
  } finally {
    connecting.value = false
  }
}

// ---- Git 设置（连接之后随时改：GitLab 令牌、提交身份、默认推送方式）----
const showGit = ref(false)
const gitForm = ref({ token: '', git_name: '', git_email: '', push_mode: 'confirm' })
const savingGit = ref(false)
function openGit() {
  const p = profile.value
  gitForm.value = { token: '', git_name: p.git_name, git_email: p.git_email, push_mode: p.push_mode }
  showGit.value = true
}
async function saveGit() {
  savingGit.value = true
  try {
    await api('/api/work/connect', { method: 'POST', body: { ...gitForm.value, connector_id: profile.value.connector_id } })
    message.success(gitForm.value.token ? t('已保存，新令牌已生效') : t('已保存'))
    showGit.value = false
    await load()
  } catch (e) {
    message.error(e.message)
  } finally {
    savingGit.value = false
  }
}

// ---- 目录与仓库 ----
const projectOptions = ref([])
const searchingProjects = ref(false)
let projectTimer = null
function searchProjects(q) {
  clearTimeout(projectTimer)
  projectTimer = setTimeout(async () => {
    searchingProjects.value = true
    try {
      const list = (await api(`/api/work/gitlab/projects?q=${encodeURIComponent(q || '')}`)).projects
      projectOptions.value = list.map(p => ({ label: `${p.path}${p.description ? ' · ' + p.description : ''}`, value: p.url, branch: p.default_branch }))
    } catch (e) {
      projectOptions.value = []
      if (q) message.warning(e.message)
    } finally {
      searchingProjects.value = false
    }
  }, 300)
}
function pickProject(url, option) {
  repoForm.value.url = url
  if (option?.branch) repoForm.value.branch = option.branch
}
const showRepo = ref(false)
const repoForm = ref({ dir_id: null, new_dir: '', new_desc: '', url: '', branch: 'main' })
const addingRepo = ref(false)
const dirOptions = computed(() => [
  ...home.value.dirs.map(d => ({ label: d.name, value: d.id })),
  { label: t('＋ 新建目录'), value: 0 },
])
function openRepo(dirId = null) {
  repoForm.value = { dir_id: dirId ?? home.value.dirs[0]?.id ?? 0, new_dir: '', new_desc: '', url: '', branch: 'main' }
  showRepo.value = true
  if (profile.value?.has_token) searchProjects('')
}
async function addRepo() {
  addingRepo.value = true
  try {
    let dirId = repoForm.value.dir_id
    if (!dirId) {
      const d = await api('/api/work/dirs', { method: 'POST', body: { name: repoForm.value.new_dir, description: repoForm.value.new_desc } })
      dirId = d.id
    }
    await api(`/api/work/dirs/${dirId}/repos`, { method: 'POST', body: { url: repoForm.value.url, branch: repoForm.value.branch } })
    message.success(t('已克隆到目录'))
    showRepo.value = false
    await load()
  } catch (e) {
    message.error(e.message)
  } finally {
    addingRepo.value = false
  }
}
const showDir = ref(false)
const dirForm = ref({ id: 0, name: '', description: '' })
function editDir(d) {
  dirForm.value = { id: d.id, name: d.name, description: d.description }
  branchDraft.value = Object.fromEntries(d.repos.map(r => [r.id, r.branch]))
  showDir.value = true
}
async function saveDir() {
  try {
    await api(`/api/work/dirs/${dirForm.value.id}`, { method: 'PUT', body: dirForm.value })
    showDir.value = false
    await load()
  } catch (e) {
    message.error(e.message)
  }
}
const branchDraft = ref({})
const savingBranch = ref(0)
async function saveBranch(repo) {
  savingBranch.value = repo.id
  try {
    await api(`/api/work/repos/${repo.id}`, { method: 'PUT', body: { branch: branchDraft.value[repo.id] } })
    message.success(t('以后新会话从 {branch} 开始', { branch: branchDraft.value[repo.id] }))
    await load()
  } catch (e) {
    message.error(e.message)
  } finally {
    savingBranch.value = 0
  }
}
async function removeRepo(repo) {
  try {
    await api(`/api/work/repos/${repo.id}`, { method: 'DELETE' })
    await load()
  } catch (e) {
    message.error(e.message)
  }
}

// ---- 会话 ----
const agents = computed(() => home.value.servers.find(s => s.id === profile.value?.connector_id)?.agents || [])
const showNew = ref(false)
const newForm = ref({ agent_id: null, dir_id: null, prompt: '', push_mode: 'confirm' })
const newImages = ref([])
const images = ref([])
const imagePayload = list => list.map(({ name, data }) => ({ name, data }))
const creating = ref(false)
function openNew() {
  newForm.value = {
    agent_id: agents.value.find(a => a.online)?.id ?? null, dir_id: home.value.dirs.find(d => d.repos.length)?.id ?? null,
    prompt: '', push_mode: profile.value?.push_mode || 'confirm',
  }
  showNew.value = true
}
async function createSession() {
  creating.value = true
  try {
    const s = await api('/api/work/sessions', { method: 'POST', body: { ...newForm.value, images: imagePayload(newImages.value) } })
    newImages.value = []
    showNew.value = false
    await load()
    select(s.id)
  } catch (e) {
    message.error(e.message)
  } finally {
    creating.value = false
  }
}
const running = computed(() => home.value.sessions.filter(s => ACTIVE_STATUSES.includes(s.status)).length)

const selectedId = ref(null)
const current = ref(null)
const events = ref([])
const draft = ref('')
const sending = ref(false)
const scroller = ref(null)
let close = null
let lastId = 0

function select(id) {
  if (selectedId.value === id) return
  selectedId.value = id
  current.value = home.value.sessions.find(s => s.id === id) || null
  events.value = []
  lastId = 0
  changes.value = null
  close?.()
  close = openStream(() => `/api/work/sessions/${id}/stream?after=${lastId}`, (name, payload) => {
    if (name !== 'session') return
    const wasActive = current.value && ACTIVE_STATUSES.includes(current.value.status)
    current.value = payload.session
    if (payload.events.length) {
      events.value.push(...payload.events)
      lastId = payload.events[payload.events.length - 1].id
      scrollDown()
    }
    const row = home.value.sessions.find(s => s.id === id)
    if (row) Object.assign(row, payload.session)
    if (!changes.value || (wasActive && !ACTIVE_STATUSES.includes(payload.session.status))) loadChanges()
  })
}
async function scrollDown() {
  await nextTick()
  const el = scroller.value
  if (el) el.scrollTop = el.scrollHeight
}
const busy = computed(() => current.value && ACTIVE_STATUSES.includes(current.value.status))
async function send() {
  if (!draft.value.trim() && !images.value.length) return
  sending.value = true
  try {
    await api(`/api/work/sessions/${selectedId.value}/continue`, { method: 'POST', body: { message: draft.value, images: imagePayload(images.value) } })
    draft.value = ''
    images.value = []
  } catch (e) {
    message.error(e.message)
  } finally {
    sending.value = false
  }
}
// 回车发送，Shift+回车换行；输入法选字时的回车不算
function onEnter(e) {
  if (e.shiftKey || e.isComposing || e.keyCode === 229) return
  e.preventDefault()
  if (!sending.value) send()
}
async function cancel() {
  try {
    await api(`/api/work/sessions/${selectedId.value}/cancel`, { method: 'POST' })
  } catch (e) {
    message.error(e.message)
  }
}
async function removeSession(force = false) {
  try {
    await api(`/api/work/sessions/${selectedId.value}${force ? '?force=1' : ''}`, { method: 'DELETE' })
    message.success(t('会话已删除，它的分支仍保留在仓库里'))
    close?.()
    selectedId.value = null
    current.value = null
    await load()
  } catch (e) {
    if (!force && e.message.includes('未推送')) {
      dialog.warning({ title: t('还有没推送的改动'), content: e.message, positiveText: t('仍然删除'), negativeText: t('取消'),
        onPositiveClick: () => removeSession(true) })
    } else {
      message.error(e.message)
    }
  }
}
async function setMode(mode) {
  try {
    current.value = { ...current.value, ...(await api(`/api/work/sessions/${selectedId.value}/mode`, { method: 'POST', body: { push_mode: mode } })) }
  } catch (e) {
    message.error(e.message)
  }
}

// 对话里只展示同事关心的内容：提问、智能体的过程（工具调用）、结果和错误
const shown = computed(() => events.value.filter(e => ['prompt', 'tool', 'output', 'result', 'error'].includes(e.kind) ||
  (e.kind === 'image' && JSON.parse(e.content).input)))
const imageFile = e => [{ id: e.id, kind: 'image', ...JSON.parse(e.content) }]

// ---- 改动与推送 ----
const changes = ref(null)
const loadingChanges = ref(false)
const pushing = ref('')
async function loadChanges() {
  if (!selectedId.value) return
  loadingChanges.value = true
  try {
    changes.value = await api(`/api/work/sessions/${selectedId.value}/changes`)
    // a session that moved to another branch moves the directory with it (the hub records it); show that at once
    const dir = home.value.dirs.find(d => d.id === current.value?.dir_id)
    if (dir && changes.value.repos.some(r => r.base && dir.repos.some(x => x.name === r.repo && x.branch !== r.base))) load()
  } catch (e) {
    changes.value = { repos: [], error: e.message }
  } finally {
    loadingChanges.value = false
  }
}
const unpushed = computed(() => (changes.value?.repos || []).filter(r => r.unpushed > 0))
async function push(repos) {
  pushing.value = repos ? repos[0] : '*'
  try {
    const r = await api(`/api/work/sessions/${selectedId.value}/push`, { method: 'POST', body: { repos: repos || [] } })
    const ok = r.pushed.filter(p => p.pushed).map(p => `${p.repo} → ${p.branch}`)
    const bad = r.pushed.filter(p => p.error)
    if (ok.length) message.success(t('已推送 {list}', { list: ok.join(t('、')) }))
    for (const p of bad) message.error(t('{repo} 推送失败：{error}', { repo: p.repo, error: p.error }))
    if (!ok.length && !bad.length) message.info(t('没有新的提交'))
    await loadChanges()
  } catch (e) {
    message.error(e.message)
  } finally {
    pushing.value = ''
  }
}

onMounted(() => {
  load()
  if (route.query.gitlab === 'ok') message.success(t('已授权 GitLab'))
  if (route.query.gitlab_error) message.error(String(route.query.gitlab_error))
  if (route.query.gitlab || route.query.gitlab_error) router.replace({ query: {} })
})
onBeforeUnmount(() => close?.())
</script>

<template>
  <div class="work-page">
    <PageHeader :title="t('工作')" :description="t('在工作服务器上只属于你的独立容器里，和智能体对话写代码，用你自己的 GitLab 账号提交和推送。')" />

    <n-spin v-if="loading" />

    <!-- 首次连接 -->
    <n-card v-else-if="!profile" :title="t('建立连接')" class="connect-card">
      <n-empty v-if="!home.servers.length" :description="t('还没有可用的工作服务器，请联系管理员部署。')" />
      <n-form v-else label-placement="top" :disabled="connecting">
        <n-form-item :label="t('工作服务器')">
          <n-select v-model:value="connect.connector_id" :options="serverOptions" />
        </n-form-item>
        <n-form-item v-if="home.oauth" :label="t('GitLab 授权')">
          <n-space align="center">
            <n-button v-if="!home.gitlab_pending" type="primary" secondary @click="gitlabAuthorize">{{ t('前往 GitLab 授权') }}</n-button>
            <n-tag v-else type="success" :bordered="false">{{ t('已授权 GitLab · {user}', { user: home.gitlab_pending.username }) }}</n-tag>
            <n-text depth="3">{{ t('或在下面填写个人访问令牌') }}</n-text>
          </n-space>
        </n-form-item>
        <n-form-item :label="t('GitLab 个人访问令牌（api、read_repository、write_repository）')">
          <n-input v-model:value="connect.token" type="password" show-password-on="click" :placeholder="home.gitlab_pending ? t('已授权，可不填') : t('glpat-…（建合并请求需要 api 权限）')" />
        </n-form-item>
        <n-text depth="3" class="hint">{{ t('令牌加密保存在 Hub，只在克隆、拉取、推送时临时交给 git，不会放进容器，智能体看不到。') }}</n-text>
        <n-grid :cols="2" :x-gap="16" responsive="screen" item-responsive>
          <n-form-item-gi span="2 m:1" :label="t('提交姓名（user.name）')"><n-input v-model:value="connect.git_name" /></n-form-item-gi>
          <n-form-item-gi span="2 m:1" :label="t('提交邮箱（user.email）')"><n-input v-model:value="connect.git_email" /></n-form-item-gi>
        </n-grid>
        <n-form-item :label="t('新会话默认推送方式')">
          <n-radio-group v-model:value="connect.push_mode">
            <n-radio value="confirm">{{ t('确认后推送（智能体 commit，我看过改动再推）') }}</n-radio>
            <n-radio value="direct">{{ t('直接推送（每轮完成后自动推到会话分支）') }}</n-radio>
          </n-radio-group>
        </n-form-item>
        <n-button type="primary" :loading="connecting" @click="doConnect">{{ t('开通我的容器') }}</n-button>
      </n-form>
    </n-card>

    <!-- 工作台 -->
    <div v-else class="bench">
      <aside class="bench-left">
        <n-alert v-if="!profile.has_token" type="warning" :show-icon="false" class="git-alert">
          {{ t('还没有设置 GitLab 凭证，克隆、推送和建合并请求都用不了。') }}
          <n-button size="small" type="warning" secondary @click="openGit">{{ t('去设置') }}</n-button>
        </n-alert>
        <n-button type="primary" block @click="openNew"><template #icon><Plus /></template>{{ t('新建会话') }}</n-button>
        <div class="side-head"><span>{{ t('我的目录与仓库') }}</span><n-button text type="primary" size="small" @click="openRepo()">{{ t('添加仓库') }}</n-button></div>
        <n-empty v-if="!home.dirs.length" size="small" :description="t('先添加一个仓库')" />
        <div v-for="d in home.dirs" :key="d.id" class="dir-card">
          <div class="dir-title">
            <span><FolderGit2 :size="14" /> {{ d.name }}</span>
            <n-button text size="tiny" @click="editDir(d)">{{ t('编辑') }}</n-button>
          </div>
          <div v-if="d.description" class="dir-desc">{{ d.description }}</div>
          <div v-for="r in d.repos" :key="r.id" class="repo-row" :title="t('当前分支 {branch}，新会话也从这里开始', { branch: r.branch })"><span>{{ r.name }}</span><code>{{ r.branch }}</code></div>
        </div>
        <div class="side-head"><span>{{ t('会话 · 运行中 {n} / 3', { n: running }) }}</span></div>
        <button v-for="s in home.sessions" :key="s.id" type="button" :class="['session-item', { active: s.id === selectedId }]" @click="select(s.id)">
          <span class="session-top"><span class="session-title">{{ s.title }}</span><n-tag size="tiny" :type="STATUS[s.status]?.type" :bordered="false">{{ STATUS[s.status]?.label || s.status }}</n-tag></span>
          <span class="session-meta"><n-tag size="tiny" :bordered="false">{{ s.agent_name }}</n-tag>{{ s.dir_name }}</span>
        </button>
        <div class="container-note">
          <n-button size="small" quaternary @click="openGit"><template #icon><KeyRound /></template>{{ t('Git 设置') }}{{ profile.has_token ? '' : ' ' + t('（未设置凭证）') }}</n-button>
          <div>{{ t('容器 {name}', { name: profile.container }) }} · {{ profile.server?.name }}</div>
        </div>
      </aside>

      <main class="bench-main">
        <n-empty v-if="!current" :description="t('选择左侧会话，或新建一个')" class="main-empty" />
        <template v-else>
          <header class="thread-head">
            <div>
              <h3>{{ current.title }}</h3>
              <div class="thread-meta">
                <n-tag size="small" :bordered="false">{{ current.agent_name }}</n-tag>
                <span>{{ t('目录 {name}', { name: current.dir_name }) }}</span>
                <n-tag size="small" :type="STATUS[current.status]?.type" :bordered="false">{{ STATUS[current.status]?.label }}</n-tag>
              </div>
            </div>
            <n-space size="small" align="center">
              <n-radio-group :value="current.push_mode" size="small" @update:value="setMode">
                <n-radio-button value="confirm">{{ t('确认后推送') }}</n-radio-button>
                <n-radio-button value="direct">{{ t('直接推送') }}</n-radio-button>
              </n-radio-group>
              <n-popconfirm @positive-click="removeSession(false)">
                <template #trigger><n-button size="small" quaternary :disabled="busy" :aria-label="t('删除会话')"><template #icon><Trash2 /></template></n-button></template>
                {{ t('删除会话会移除它在容器里的工作副本，分支仍保留在仓库里。') }}
              </n-popconfirm>
            </n-space>
          </header>
          <div ref="scroller" class="thread">
            <template v-for="e in shown" :key="e.id">
              <div v-if="e.kind === 'prompt'" class="bubble me">{{ e.content }}</div>
              <div v-else-if="e.kind === 'image'" class="sent-image"><TaskFiles :task-id="current.task_id" :files="imageFile(e)" /></div>
              <div v-else-if="e.kind === 'tool'" class="tool-line">{{ e.content }}</div>
              <div v-else-if="e.kind === 'result'" class="bubble agent"><MarkdownText :text="e.content" /></div>
              <div v-else-if="e.kind === 'error'" class="bubble error">{{ e.content }}</div>
              <div v-else class="out-line">{{ e.content }}</div>
            </template>
            <div v-if="busy" class="working"><n-spin size="small" /> {{ t('{name} 正在处理…', { name: current.agent_name }) }}</div>
          </div>
          <div class="composer">
            <ImageAttach v-model="images" :disabled="busy">
              <n-input v-model:value="draft" type="textarea" :autosize="{ minRows: 2, maxRows: 6 }" :placeholder="t('继续对话，比如：再跑一遍测试（回车发送，Shift+回车换行，可粘贴截图）')" :disabled="busy" @keydown.enter="onEnter" />
            </ImageAttach>
            <n-button v-if="busy" @click="cancel"><template #icon><Square /></template>{{ t('停止') }}</n-button>
            <n-button v-else type="primary" :loading="sending" @click="send"><template #icon><Send /></template>{{ t('发送') }}</n-button>
          </div>
        </template>
      </main>

      <aside v-if="current" class="bench-right">
        <div class="side-head"><span>{{ t('改动 · 按仓库') }}</span><n-button text size="small" :loading="loadingChanges" @click="loadChanges"><template #icon><RefreshCw /></template></n-button></div>
        <n-alert v-if="changes?.error" type="error" :show-icon="false">{{ changes.error }}</n-alert>
        <div v-for="r in changes?.repos || []" :key="r.repo" class="repo-change">
          <div class="dir-title"><span><GitBranch :size="14" /> {{ r.repo }}</span>
            <n-tag size="tiny" :bordered="false" :type="!r.ready ? 'default' : r.unpushed ? 'warning' : 'default'">
              {{ !r.ready ? t('未准备') : r.unpushed ? t('待推送 {n}', { n: r.unpushed }) : t('没有待推送') }}
            </n-tag>
          </div>
          <div v-if="r.base" class="dir-desc">{{ t('分支') }} <code>{{ r.base }}</code></div>
          <div v-for="f in r.files" :key="f.path" class="file-row"><code>{{ f.status }}</code> {{ f.path }}</div>
          <div v-if="r.files.length" class="dir-desc">{{ t('以上为未提交的改动') }}</div>
          <div v-for="c in r.commits" :key="c.sha" class="commit-row"><code>{{ c.sha }}</code> {{ c.subject }} <span class="dir-desc">· {{ c.author }}</span></div>
          <n-button v-if="r.unpushed && current.push_mode === 'confirm'" size="small" secondary type="primary" :disabled="busy" :loading="pushing === r.repo" @click="push([r.repo])">{{ t('推送这个仓库') }}</n-button>
        </div>
        <div class="push-foot">
          <n-text depth="3">{{ t('提交推送到各仓库所在的分支（见上方），作者 {name}。远端有别人的新提交时会先提示你拉取。', { name: profile.git_name }) }}</n-text>
          <n-button v-if="current.push_mode === 'confirm'" type="primary" block :disabled="busy || !unpushed.length" :loading="pushing === '*'" @click="push(null)">
            <template #icon><Upload /></template>{{ t('全部推送（{n} 个仓库）', { n: unpushed.length }) }}
          </n-button>
          <n-text v-else depth="3">{{ t('直接推送：每轮完成后自动推送。') }}</n-text>
        </div>
      </aside>
    </div>

    <n-modal v-model:show="showGit" preset="card" :title="t('Git 设置')" style="max-width: 560px">
      <n-form label-placement="top" :disabled="savingGit">
        <n-form-item :label="t('GitLab 凭证')">
          <n-space align="center">
            <n-tag :type="profile?.has_token ? 'success' : 'warning'" :bordered="false">
              {{ profile?.has_token ? (profile.token_kind === 'oauth' ? t('已通过 GitLab 授权') : t('已设置个人访问令牌')) : t('未设置') }}
            </n-tag>
            <n-button v-if="home.oauth" size="small" secondary type="primary" @click="gitlabAuthorize">{{ profile?.token_kind === 'oauth' ? t('重新授权') : t('前往 GitLab 授权') }}</n-button>
          </n-space>
        </n-form-item>
        <n-form-item :label="profile?.has_token ? t('换一个个人访问令牌（不换就留空）') : t('GitLab 个人访问令牌（api、read_repository、write_repository）')">
          <n-input v-model:value="gitForm.token" type="password" show-password-on="click" placeholder="glpat-…" />
        </n-form-item>
        <n-text depth="3" class="hint">{{ t('在 GitLab「偏好设置 → 访问令牌」生成，勾选 api、read_repository、write_repository。令牌加密保存在 Hub，只在克隆、拉取、推送时临时交给 git，不会放进容器，智能体看不到。') }}</n-text>
        <n-grid :cols="2" :x-gap="16" responsive="screen" item-responsive>
          <n-form-item-gi span="2 m:1" :label="t('提交姓名（user.name）')"><n-input v-model:value="gitForm.git_name" /></n-form-item-gi>
          <n-form-item-gi span="2 m:1" :label="t('提交邮箱（user.email）')"><n-input v-model:value="gitForm.git_email" /></n-form-item-gi>
        </n-grid>
        <n-form-item :label="t('新会话默认推送方式')">
          <n-radio-group v-model:value="gitForm.push_mode">
            <n-radio value="confirm">{{ t('确认后推送') }}</n-radio><n-radio value="direct">{{ t('直接推送') }}</n-radio>
          </n-radio-group>
        </n-form-item>
      </n-form>
      <template #footer><n-space justify="end"><n-button @click="showGit = false">{{ t('取消') }}</n-button><n-button type="primary" :loading="savingGit" @click="saveGit">{{ t('保存') }}</n-button></n-space></template>
    </n-modal>

    <n-modal v-model:show="showNew" preset="card" :title="t('新建会话')" style="max-width: 560px">
      <n-form label-placement="top">
        <n-form-item :label="t('选择智能体')">
          <n-radio-group v-model:value="newForm.agent_id">
            <n-space vertical>
              <n-radio v-for="a in agents" :key="a.id" :value="a.id" :disabled="!a.online">{{ a.name }}<n-text depth="3"> {{ a.description }}</n-text></n-radio>
            </n-space>
          </n-radio-group>
        </n-form-item>
        <n-form-item :label="t('在哪个目录里工作')">
          <n-radio-group v-model:value="newForm.dir_id">
            <n-space vertical>
              <n-radio v-for="d in home.dirs" :key="d.id" :value="d.id" :disabled="!d.repos.length">
                {{ d.name }}<n-text depth="3"> {{ d.repos.map(r => r.name).join(t('、')) || t('（没有仓库）') }}</n-text>
              </n-radio>
            </n-space>
          </n-radio-group>
        </n-form-item>
        <n-form-item :label="t('要智能体做什么')">
          <ImageAttach v-model="newImages"><n-input v-model:value="newForm.prompt" type="textarea" :autosize="{ minRows: 3 }" :placeholder="t('可粘贴截图')" /></ImageAttach>
        </n-form-item>
        <n-form-item :label="t('推送方式')">
          <n-radio-group v-model:value="newForm.push_mode">
            <n-radio value="confirm">{{ t('确认后推送') }}</n-radio><n-radio value="direct">{{ t('直接推送') }}</n-radio>
          </n-radio-group>
        </n-form-item>
        <n-text depth="3">{{ t('每个仓库从它当前的分支（目录里显示的）拉最新代码开始，提交也推回这个分支。会话开始后不能更换智能体。') }}</n-text>
      </n-form>
      <template #footer><n-space justify="end"><n-button @click="showNew = false">{{ t('取消') }}</n-button><n-button type="primary" :loading="creating" @click="createSession">{{ t('开始会话') }}</n-button></n-space></template>
    </n-modal>

    <n-modal v-model:show="showRepo" preset="card" :title="t('添加仓库')" style="max-width: 560px">
      <n-form label-placement="top" :disabled="addingRepo">
        <n-form-item v-if="profile?.has_token" :label="t('从 GitLab 选择项目')">
          <n-select :value="null" filterable remote clearable :options="projectOptions" :loading="searchingProjects" :placeholder="t('搜索你有权限的项目')" @search="searchProjects" @update:value="pickProject" />
        </n-form-item>
        <n-form-item :label="t('仓库地址')"><n-input v-model:value="repoForm.url" :placeholder="t('https://gitlab.example.com/group/project.git')" /></n-form-item>
        <n-form-item :label="t('分支')"><n-input v-model:value="repoForm.branch" /></n-form-item>
        <n-form-item :label="t('放到哪个目录')"><n-select v-model:value="repoForm.dir_id" :options="dirOptions" /></n-form-item>
        <template v-if="!repoForm.dir_id">
          <n-form-item :label="t('目录名（只用于显示，可以用中文，随时可改）')"><n-input v-model:value="repoForm.new_dir" /></n-form-item>
          <n-form-item :label="t('目录说明（可选，会告诉智能体）')"><n-input v-model:value="repoForm.new_desc" type="textarea" :autosize="{ minRows: 2 }" /></n-form-item>
        </template>
        <n-text depth="3">{{ t('用你的 GitLab 令牌克隆到你自己的容器里，同事看不到。') }}</n-text>
      </n-form>
      <template #footer><n-space justify="end"><n-button @click="showRepo = false">{{ t('取消') }}</n-button><n-button type="primary" :loading="addingRepo" @click="addRepo">{{ t('克隆到目录') }}</n-button></n-space></template>
    </n-modal>

    <n-modal v-model:show="showDir" preset="card" :title="t('编辑目录')" style="max-width: 520px">
      <n-form label-placement="top">
        <n-form-item :label="t('目录名')"><n-input v-model:value="dirForm.name" /></n-form-item>
        <n-form-item :label="t('目录说明（会告诉智能体）')"><n-input v-model:value="dirForm.description" type="textarea" :autosize="{ minRows: 2 }" /></n-form-item>
        <n-form-item :label="t('仓库和当前分支')">
          <n-space vertical style="width: 100%">
            <div v-for="r in home.dirs.find(d => d.id === dirForm.id)?.repos || []" :key="r.id" class="repo-edit">
              <span class="repo-edit__name">{{ r.name }}</span>
              <n-input v-model:value="branchDraft[r.id]" size="small" :placeholder="t('如 feature/v1.4.0')" />
              <n-button size="small" secondary type="primary" :disabled="!branchDraft[r.id] || branchDraft[r.id] === r.branch" :loading="savingBranch === r.id" @click="saveBranch(r)">{{ t('改分支') }}</n-button>
              <n-button text type="error" size="small" @click="removeRepo(r)">{{ t('移除') }}</n-button>
            </div>
          </n-space>
        </n-form-item>
        <n-text depth="3">{{ t('改名只改显示，不移动文件。在对话里让智能体切到别的分支，这里会自动跟着变；也可以在这里直接改。新会话从当前分支开始，已有会话不受影响。') }}</n-text>
      </n-form>
      <template #footer><n-space justify="end"><n-button @click="showDir = false">{{ t('取消') }}</n-button><n-button type="primary" @click="saveDir">{{ t('保存') }}</n-button></n-space></template>
    </n-modal>
  </div>
</template>

<style scoped>
.connect-card { max-width: 720px; }
.hint { display: block; margin: -8px 0 16px; font-size: 12px; }
.bench { display: grid; grid-template-columns: 260px minmax(0, 1fr) 300px; gap: 12px; min-height: calc(100vh - 170px); }
.bench-left, .bench-right { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.bench-main { display: flex; flex-direction: column; min-width: 0; border: 1px solid var(--n-border-color, rgba(128,128,128,.2)); border-radius: 10px; background: var(--card-color, transparent); }
.side-head { display: flex; justify-content: space-between; align-items: center; font-size: 12px; font-weight: 600; opacity: .75; margin-top: 6px; }
.dir-card, .repo-change { border: 1px solid rgba(128,128,128,.22); border-radius: 8px; padding: 8px 10px; display: flex; flex-direction: column; gap: 4px; }
.dir-title { display: flex; justify-content: space-between; align-items: center; gap: 6px; font-size: 13px; font-weight: 600; }
.dir-title span { display: inline-flex; align-items: center; gap: 4px; }
.dir-desc { font-size: 12px; opacity: .7; }
.repo-edit { display: flex; align-items: center; gap: 8px; }
.repo-edit__name { flex: 0 0 auto; font-size: 13px; }
.repo-row { display: flex; justify-content: space-between; gap: 6px; font-size: 12px; padding-left: 4px; }
.repo-row code, .thread-meta code, .file-row code, .commit-row code, .push-foot code { font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 11.5px; opacity: .8; }
.session-item { display: flex; flex-direction: column; gap: 4px; text-align: left; padding: 8px 10px; border-radius: 8px; border: 1px solid transparent; background: transparent; color: inherit; cursor: pointer; font: inherit; min-height: 44px; }
.session-item:hover { background: rgba(128,128,128,.08); }
.session-item.active { border-color: var(--primary-color, #18a058); background: rgba(24,160,88,.08); }
.session-top { display: flex; justify-content: space-between; gap: 6px; }
.session-title { font-size: 13px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.session-meta { display: flex; gap: 6px; align-items: center; font-size: 12px; opacity: .8; }
.container-note { margin-top: auto; display: flex; flex-direction: column; align-items: flex-start; gap: 4px; font-size: 12px; }
.container-note > div { opacity: .6; }
.git-alert :deep(.n-alert-body__content) { display: flex; flex-direction: column; align-items: flex-start; gap: 8px; font-size: 13px; }
.main-empty { margin: auto; }
.thread-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; padding: 12px 16px; border-bottom: 1px solid rgba(128,128,128,.2); }
.thread-head h3 { margin: 0 0 4px; font-size: 15px; }
.thread-meta { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; font-size: 12px; opacity: .85; }
.thread { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 10px; max-height: calc(100vh - 330px); }
.bubble { max-width: 86%; padding: 10px 14px; border-radius: 12px; font-size: 14px; line-height: 1.6; white-space: pre-wrap; }
.bubble.me { align-self: flex-end; background: rgba(24,160,88,.12); }
.bubble.agent { background: rgba(128,128,128,.1); white-space: normal; }
.bubble.error { background: rgba(208,48,80,.1); color: #d03050; }
.tool-line, .out-line { font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 12px; opacity: .75; white-space: pre-wrap; word-break: break-all; }
.sent-image { align-self: flex-end; }
.working { display: flex; gap: 8px; align-items: center; font-size: 13px; opacity: .8; }
.composer { display: flex; gap: 8px; align-items: flex-end; padding: 12px 16px; border-top: 1px solid rgba(128,128,128,.2); }
.file-row, .commit-row { font-size: 12px; word-break: break-all; }
.push-foot { margin-top: auto; display: flex; flex-direction: column; gap: 8px; font-size: 12px; }
@media (max-width: 1100px) {
  .bench { grid-template-columns: 1fr; }
  .thread { max-height: 60vh; }
}
</style>
