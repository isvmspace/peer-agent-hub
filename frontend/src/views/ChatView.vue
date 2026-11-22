<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useDialog, useMessage } from 'naive-ui'
import { Bot, KeyRound, MessageCircle, MessageSquarePlus, Plus, Send, Trash2, UserPlus, Users } from '@lucide/vue'
import ChatThread from '../components/ChatThread.vue'
import ImageAttach from '../components/ImageAttach.vue'
import GroupChat from '../components/GroupChat.vue'
import DirectChat from '../components/DirectChat.vue'
import StatusTag from '../components/StatusTag.vue'
import { api } from '../api/client'
import { openStream } from '../api/stream'
import { useAuth } from '../composables/useAuth'
import { ISOLATION, formatTime } from '../utils/format'

import { useDraft } from '../composables/useDraft'
import { t } from '../i18n'
const route = useRoute()
const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const { state: auth } = useAuth()
const contacts = reactive({ mine: [], friends: [], outgoing: [], incoming: [], followers: [] })
const grant = reactive({ show: false, item: null, accept: false, trust: false, workspaces: [], allowFull: false })
const people = reactive({ friends: [], outgoing: [], incoming: [] })
const users = ref([])
const groups = ref([])
const allAgents = ref([])
const threads = ref([])
const tab = ref('friends')
const busy = ref(false)
const showAdd = ref(false)
const addForm = reactive({ agent: null, note: '' })
const showFriend = ref(false)
const friendForm = reactive({ user: null, note: '' })
const showGroup = ref(false)
const groupForm = reactive({ id: null, name: '', people: [], members: [] })
const draft = reactive({ workspace: '' })
// the first message to an agent is kept per agent, like every other unsent draft
const { draft: newPrompt, clear: clearNewPrompt } = useDraft(() => `new:${agentId.value}`)
const newImages = ref([])

const agentId = computed(() => (route.query.agent ? Number(route.query.agent) : null))
const taskId = computed(() => (route.query.task ? Number(route.query.task) : null))
const groupId = computed(() => (route.query.group ? Number(route.query.group) : null))
const dmId = computed(() => (route.query.dm ? Number(route.query.dm) : null))
const currentFriend = computed(() => people.friends.find(f => f.user_id === dmId.value))
const contactAgents = computed(() => [...contacts.mine, ...contacts.friends.map(f => f.agent)])
const currentAgent = computed(() => contactAgents.value.find(a => a.id === agentId.value) || allAgents.value.find(a => a.id === agentId.value))
const currentGroup = computed(() => groups.value.find(g => g.id === groupId.value))
function workspaceChoices(agent) {
  if (!agent) return []
  const own = agent.user_id === auth.user?.id
  const items = (agent.workspaces || []).map(w => ({ label: own ? `${w.name} · ${w.path}` : w.name, value: w.id }))
  return own ? [{ label: agent.workdir ? t('默认目录（{dir}）', { dir: agent.workdir }) : t('默认目录'), value: '' }, ...items] : items
}
const workspaceOptions = computed(() => workspaceChoices(currentAgent.value))
const isOwnAgent = computed(() => currentAgent.value?.user_id === auth.user?.id)
// access can only be requested for agents of people who are already friends
const addOptions = computed(() => {
  const friendIds = new Set(people.friends.map(f => f.user_id))
  return allAgents.value
    .filter(a => friendIds.has(a.user_id) && a.access === 'none')
    .map(a => ({ label: `${a.owner_name} / ${a.name}（${a.address}）`, value: a.address }))
})
const friendOptions = computed(() => {
  const known = new Set([auth.user?.id, ...people.friends.map(f => f.user_id), ...people.outgoing.map(o => o.user_id), ...people.incoming.map(o => o.user_id)])
  return users.value.filter(u => !known.has(u.id)).map(u => ({ label: `${u.display_name}（${u.username}）`, value: u.username }))
})
const peopleOptions = computed(() => people.friends.map(f => ({ label: `${f.display_name || f.username}（${f.username}）`, value: f.username })))
const memberOptions = computed(() => contactAgents.value.map(a => ({ label: `${a.name}（${a.address}）`, value: a.id })))
const groupUnread = computed(() => groups.value.reduce((n, g) => n + (g.id === groupId.value ? 0 : g.unread || 0), 0))
const unreadTotal = computed(() => people.friends.reduce((n, f) => n + (f.unread || 0), 0))

async function loadContacts() {
  try {
    const [c, g, a, f, u] = await Promise.all([api('/api/contacts'), api('/api/groups'), api('/api/agents'), api('/api/friends'), api('/api/users')])
    Object.assign(contacts, c)
    Object.assign(people, f)
    users.value = u.users
    groups.value = g.groups
    allAgents.value = a.agents
  } catch (e) { message.error(e.message) }
}

async function loadThreads() {
  const forAgent = agentId.value
  if (!forAgent) { threads.value = []; return }
  try {
    const rows = (await api(`/api/agents/${forAgent}/conversations`)).tasks
    // a slow answer for an agent the user already left must not replace the current list
    if (agentId.value === forAgent) threads.value = rows
  } catch (e) { message.error(e.message) }
}

function go(query) { router.replace({ name: 'chat', query }) }

async function startConversation() {
  if (busy.value || (!newPrompt.value.trim() && !newImages.value.length)) return
  const sentFrom = agentId.value
  busy.value = true
  try {
    const sent = newPrompt.value
    const images = newImages.value.map(({ name, data }) => ({ name, data }))
    const { task } = await api('/api/tasks', { method: 'POST', body: { agent: sentFrom, prompt: sent, workspace: draft.workspace, images } })
    newImages.value = []
    if (task.status === 'pending_approval') message.info(t('已发送，等待对方审批'))
    clearNewPrompt(`new:${sentFrom}`, sent)
    // the user may have moved to another agent while this was sending
    if (agentId.value !== sentFrom) return
    await loadThreads()
    // checked again: the user may also have moved on while the list was loading
    if (agentId.value === sentFrom && !taskId.value) go({ agent: sentFrom, task: task.id })
  } catch (e) { message.error(e.message) } finally { busy.value = false }
}

async function addFriend() {
  if (!friendForm.user) return
  try {
    const { friendship } = await api('/api/friends', { method: 'POST', body: { ...friendForm } })
    message.success(friendship.status === 'accepted' ? t('已成为好友') : t('已发送好友申请，等待对方同意'))
    showFriend.value = false
    friendForm.user = null
    friendForm.note = ''
    loadContacts()
  } catch (e) { message.error(e.message) }
}

async function answerFriend(item, accept) {
  try { await api(`/api/friends/${item.id}/${accept ? 'accept' : 'reject'}`, { method: 'POST' }); loadContacts() } catch (e) { message.error(e.message) }
}

function removePerson(item) {
  dialog.warning({
    title: t('删除好友'),
    content: t('确认删除好友 {name}？双方互相授予的智能体权限会一并收回，聊天记录保留。', { name: item.display_name || item.username }),
    positiveText: t('删除'),
    negativeText: t('取消'),
    onPositiveClick: async () => {
      try { await api(`/api/friends/${item.id}`, { method: 'DELETE' }); if (dmId.value === item.user_id) go({}); loadContacts() } catch (e) { message.error(e.message) }
    },
  })
}

function requestAccess(agent) {
  addForm.agent = agent?.address || null
  showAdd.value = true
}

async function addAccess() {
  if (!addForm.agent) return
  try {
    await api('/api/contacts', { method: 'POST', body: { ...addForm } })
    message.success(t('已申请使用权限，等待对方授权工作区'))
    showAdd.value = false
    addForm.agent = null
    addForm.note = ''
    loadContacts()
  } catch (e) { message.error(e.message) }
}

function answer(item, accept) {
  if (!accept) return doAnswer(item, 'reject', {})
  Object.assign(grant, { show: true, item, accept: true, trust: false, workspaces: [], allowFull: false })
}

function editGrant(item) {
  Object.assign(grant, { show: true, item, accept: false, trust: false, workspaces: [...item.granted], allowFull: !!item.allow_full })
}

async function saveGrant() {
  let ok = await doAnswer(grant.item, grant.accept ? 'accept' : 'workspaces', { trust: grant.trust, workspaces: grant.workspaces })
  // full-permission use is a separate, explicit choice; only sent when it changed
  if (ok && grant.item.agent.isolation === 'none' && !!grant.allowFull !== !!grant.item.allow_full) ok = await doAnswer(grant.item, 'full', { allow: !!grant.allowFull })
  if (ok) grant.show = false
}

async function doAnswer(item, action, body) {
  try {
    await api(`/api/contacts/${item.id}/${action}`, { method: 'POST', body })
    loadContacts()
    return true
  } catch (e) { message.error(e.message); return false }
}

function removeFriend(item) {
  dialog.warning({
    title: item.user_id === auth.user?.id ? t('放弃使用权限') : t('收回使用权限'),
    content: t('确认移除 {name} 对 {agent} 的使用权限？历史对话保留。', { name: item.display_name || item.username, agent: item.agent.address }),
    positiveText: t('删除'),
    negativeText: t('取消'),
    onPositiveClick: async () => {
      try { await api(`/api/contacts/${item.id}`, { method: 'DELETE' }); loadContacts() } catch (e) { message.error(e.message) }
    },
  })
}

function openGroupForm(group) {
  groupForm.id = group?.id || null
  groupForm.name = group?.name || ''
  groupForm.people = (group?.people || []).filter(p => !p.is_owner).map(p => p.username)
  groupForm.members = (group?.members || []).map(m => ({ agent: m.id, workspace: m.group_workspace_id || '' }))
  showGroup.value = true
}

function memberWorkspaces(id) {
  return workspaceChoices(contactAgents.value.find(a => a.id === id))
}

async function saveGroup() {
  const body = { name: groupForm.name, people: groupForm.people, members: groupForm.members.filter(m => m.agent) }
  try {
    const { group } = groupForm.id
      ? await api(`/api/groups/${groupForm.id}`, { method: 'PUT', body })
      : await api('/api/groups', { method: 'POST', body })
    showGroup.value = false
    await loadContacts()
    tab.value = 'groups'
    go({ group: group.id })
  } catch (e) { message.error(e.message) }
}

async function editCurrentGroup() {
  const detail = await api(`/api/groups/${groupId.value}`)
  openGroupForm({ ...detail.group, members: detail.members, people: detail.people })
}

function leaveGroup() {
  dialog.warning({
    title: t('退出群聊'),
    content: t('退出后看不到该群的消息。'),
    positiveText: t('退出'),
    negativeText: t('取消'),
    onPositiveClick: async () => {
      try { await api(`/api/groups/${groupId.value}/leave`, { method: 'POST' }); go({}); loadContacts() } catch (e) { message.error(e.message) }
    },
  })
}

function deleteGroup() {
  dialog.warning({
    title: t('解散群聊'),
    content: t('解散后群消息删除，各智能体的任务记录仍保留在任务中心。'),
    positiveText: t('解散'),
    negativeText: t('取消'),
    onPositiveClick: async () => {
      try { await api(`/api/groups/${groupId.value}`, { method: 'DELETE' }); go({}); loadContacts() } catch (e) { message.error(e.message) }
    },
  })
}

watch(agentId, () => { draft.workspace = workspaceOptions.value[0]?.value ?? ''; loadThreads() })
watch(workspaceOptions, options => { if (!options.some(o => o.value === draft.workspace)) draft.workspace = options[0]?.value ?? '' })
watch(groupId, id => { if (id) tab.value = 'groups' }, { immediate: true })
watch(agentId, id => { if (id) tab.value = 'agents' }, { immediate: true })
let timer = null
let closeInbox = null
let reloadTimer = null
function scheduleReload() {
  clearTimeout(reloadTimer)
  reloadTimer = setTimeout(loadContacts, 300)
}
onMounted(() => {
  loadContacts()
  loadThreads()
  timer = setInterval(loadContacts, 30000)
  closeInbox = openStream('/api/inbox/stream', name => { if (name === 'inbox') scheduleReload() }, () => {})
})
onBeforeUnmount(() => { clearInterval(timer); clearTimeout(reloadTimer); closeInbox?.() })
</script>

<template>
  <div class="chat-page">
    <aside class="chat-side">
      <n-tabs v-model:value="tab" type="segment" size="small">
        <n-tab name="friends">
          <span class="tab-label"><MessageCircle :size="14" />&nbsp;{{ t('好友') }}<n-badge :value="unreadTotal + people.incoming.length" :max="99" :show="!!(unreadTotal + people.incoming.length)" class="tab-badge" /></span>
        </n-tab>
        <n-tab name="agents">
          <span class="tab-label"><Bot :size="14" />&nbsp;{{ t('智能体') }}<n-badge :value="contacts.incoming.length" :show="!!contacts.incoming.length" class="tab-badge" /></span>
        </n-tab>
        <n-tab name="groups">
          <span class="tab-label"><Users :size="14" />&nbsp;{{ t('群聊') }}<n-badge :value="groupUnread" :max="99" :show="!!groupUnread" class="tab-badge" /></span>
        </n-tab>
      </n-tabs>

      <template v-if="tab === 'friends'">
        <n-button block dashed size="small" class="side-action" @click="showFriend = true"><template #icon><UserPlus /></template>{{ t('添加好友') }}</n-button>
        <div v-if="people.incoming.length" class="side-section">
          <div class="side-label">{{ t('好友申请') }}</div>
          <div v-for="r in people.incoming" :key="r.id" class="request-item">
            <div><b>{{ r.display_name || r.username }}</b> {{ t('想加你为好友') }}<small v-if="r.note">「{{ r.note }}」</small></div>
            <n-space size="small">
              <n-button size="tiny" type="primary" @click="answerFriend(r, true)">{{ t('同意') }}</n-button>
              <n-button size="tiny" @click="answerFriend(r, false)">{{ t('拒绝') }}</n-button>
            </n-space>
          </div>
        </div>
        <div class="side-label">{{ t('好友') }}</div>
        <div v-if="!people.friends.length" class="side-empty">{{ t('还没有好友。加好友后可以私聊、拉群，并申请使用对方的智能体') }}</div>
        <div v-for="f in people.friends" :key="f.id" class="contact-row">
          <button type="button" :class="['contact', { active: f.user_id === dmId }]" :aria-current="f.user_id === dmId ? 'true' : undefined" @click="go({ dm: f.user_id })">
            <span class="group-avatar">{{ (f.display_name || f.username).slice(0, 1) }}</span>
            <span class="contact__text"><b>{{ f.display_name || f.username }}</b><small>{{ f.last_message?.content || t('@{username} · {n} 个智能体', { username: f.username, n: f.agents.length }) }}</small></span>
            <n-badge v-if="f.unread" :value="f.unread" class="contact__badge" />
          </button>
          <n-button quaternary circle size="tiny" class="contact__del" :aria-label="t('删除好友 {name}', { name: f.display_name || f.username })" @click="removePerson(f)"><template #icon><Trash2 /></template></n-button>
        </div>
        <template v-if="people.outgoing.length">
          <div class="side-label">{{ t('等待对方同意') }}</div>
          <div v-for="o in people.outgoing" :key="o.id" class="contact muted-contact">
            <span class="group-avatar">{{ (o.display_name || o.username).slice(0, 1) }}</span><span class="contact__text"><b>{{ o.display_name || o.username }}</b><small>@{{ o.username }}</small></span>
          </div>
        </template>
      </template>

      <template v-else-if="tab === 'agents'">
        <n-button block dashed size="small" class="side-action" @click="requestAccess()"><template #icon><KeyRound /></template>{{ t('申请使用好友的智能体') }}</n-button>
        <div v-if="contacts.incoming.length" class="side-section">
          <div class="side-label">{{ t('权限申请') }}</div>
          <div v-for="r in contacts.incoming" :key="r.id" class="request-item">
            <div><b>{{ r.display_name || r.username }}</b> {{ t('申请使用你的 {agent}', { agent: r.agent.name }) }}<small v-if="r.note">「{{ r.note }}」</small></div>
            <n-space size="small">
              <n-button size="tiny" type="primary" @click="answer(r, true)">{{ t('授权') }}</n-button>
              <n-button size="tiny" @click="answer(r, false)">{{ t('拒绝') }}</n-button>
            </n-space>
          </div>
        </div>
        <div class="side-label">{{ t('我的智能体') }}</div>
        <div v-if="!contacts.mine.length" class="side-empty">{{ t('还没有接入智能体，可在「接入配置」安装连接器；没有智能体也能使用好友的智能体') }}</div>
        <div v-for="a in contacts.mine" :key="a.id" class="contact-row">
          <button type="button" :class="['contact', { active: a.id === agentId }]" :aria-current="a.id === agentId ? 'true' : undefined" @click="go({ agent: a.id })">
            <span :class="['dot', a.online ? 'on' : 'off']" /><span class="contact__text"><b>{{ a.name }}</b><small>{{ a.address }} · {{ a.hostname }}</small></span>
          </button>
        </div>
        <div class="side-label">{{ t('可使用的好友智能体') }}</div>
        <div v-if="!contacts.friends.length" class="side-empty">{{ t('先加好友，再申请使用对方的智能体') }}</div>
        <div v-for="f in contacts.friends" :key="f.id" class="contact-row">
          <button type="button" :class="['contact', { active: f.agent.id === agentId }]" :aria-current="f.agent.id === agentId ? 'true' : undefined" @click="go({ agent: f.agent.id })">
            <span :class="['dot', f.agent.online ? 'on' : 'off']" /><span class="contact__text"><b>{{ f.agent.name }}</b><small>{{ f.agent.owner_name }} · {{ f.agent.workspaces.length ? f.agent.workspaces.map(w => w.name).join(t('、')) : t('未授权工作区') }}</small></span>
          </button>
          <n-button quaternary circle size="tiny" class="contact__del" :aria-label="t('不再使用 {agent}', { agent: f.agent.name })" @click="removeFriend({ ...f, user_id: auth.user?.id })"><template #icon><Trash2 /></template></n-button>
        </div>
        <template v-if="contacts.followers.length">
          <div class="side-label">{{ t('谁能用我的智能体') }}</div>
          <div v-for="f in contacts.followers" :key="f.id" class="contact-row">
            <button type="button" class="contact" @click="editGrant(f)">
              <span class="group-avatar">{{ (f.display_name || f.username).slice(0, 1) }}</span>
              <span class="contact__text"><b>{{ f.display_name || f.username }} → {{ f.agent.name }}</b><small>{{ f.granted.length ? t('已授权 {n} 个工作区', { n: f.granted.length }) : t('未授权工作区，无法派任务') }}{{ f.agent.isolation === 'none' ? ' · ' + (f.allow_full ? t('可用完全权限') : t('完全权限，TA 暂不能用')) : '' }}</small></span>
            </button>
            <n-button quaternary circle size="tiny" class="contact__del" :aria-label="t('收回 {name} 的使用权限', { name: f.display_name || f.username })" @click="removeFriend(f)"><template #icon><Trash2 /></template></n-button>
          </div>
        </template>
        <template v-if="contacts.outgoing.length">
          <div class="side-label">{{ t('等待授权') }}</div>
          <div v-for="o in contacts.outgoing" :key="o.id" class="contact muted-contact">
            <span class="dot off" /><span class="contact__text"><b>{{ o.agent.name }}</b><small>{{ o.agent.address }}</small></span>
          </div>
        </template>
      </template>

      <template v-else>
        <n-button block dashed size="small" class="side-action" @click="openGroupForm()"><template #icon><Plus /></template>{{ t('新建群聊') }}</n-button>
        <div v-if="!groups.length" class="side-empty">{{ t('把好友和智能体拉进群，@ 谁就让谁干活') }}</div>
        <div v-for="g in groups" :key="g.id" class="contact-row">
          <button type="button" :class="['contact', { active: g.id === groupId }]" :aria-current="g.id === groupId ? 'true' : undefined" @click="go({ group: g.id })">
          <span class="group-avatar"><Users :size="14" /></span>
          <span class="contact__text"><b>{{ g.name }}</b><small>{{ g.last_message ? `${g.last_message.display_name || g.last_message.username}：${g.last_message.content}` : [...(g.people || []).map(p => p.display_name || p.username), ...g.members.map(m => m.name)].join('、') }}</small></span>
          <n-badge v-if="g.unread && g.id !== groupId" :value="g.unread" class="contact__badge" />
          </button>
        </div>
      </template>
    </aside>

    <section v-if="agentId" class="chat-threads">
      <div class="threads-head">
        <div><b>{{ currentAgent?.name }}</b><small>{{ currentAgent?.address }}</small></div>
        <n-button size="small" secondary type="primary" @click="go({ agent: agentId })"><template #icon><MessageSquarePlus /></template>{{ t('新对话') }}</n-button>
      </div>
      <button v-for="th in threads" :key="th.id" type="button" :class="['thread', { active: th.id === taskId }]" :aria-current="th.id === taskId ? 'true' : undefined" @click="go({ agent: agentId, task: th.id })">
        <span class="thread__title">{{ th.title }}</span>
        <span class="thread__meta"><StatusTag :status="th.status" /><span>{{ t('{n} 轮', { n: th.turn }) }} · {{ formatTime(th.updated_at) }}</span></span>
      </button>
      <div v-if="!threads.length" class="side-empty">{{ t('暂无对话') }}</div>
    </section>

    <main class="chat-main">
      <ChatThread v-if="taskId" :task-id="taskId" paged @loaded="loadThreads" />
      <DirectChat v-else-if="dmId && currentFriend" :friend="currentFriend" @read="scheduleReload">
        <template #actions>
          <n-space size="small">
            <template v-for="a in currentFriend.agents" :key="a.id">
              <n-button v-if="a.access === 'accepted'" size="small" secondary @click="go({ agent: a.id })"><template #icon><Bot /></template>{{ a.name }}</n-button>
              <n-button v-else-if="a.access === 'none'" size="small" dashed @click="requestAccess(a)"><template #icon><KeyRound /></template>{{ t('申请 {agent}', { agent: a.name }) }}</n-button>
              <n-tag v-else size="small" :bordered="false">{{ t('{agent} 待授权', { agent: a.name }) }}</n-tag>
            </template>
          </n-space>
        </template>
      </DirectChat>
      <GroupChat v-else-if="groupId" :group-id="groupId" @open-task="id => router.push({ name: 'task-detail', params: { id } })">
        <template #title>{{ currentGroup?.name }}</template>
        <template #actions>
          <n-space v-if="currentGroup?.owner_user_id === auth.user?.id" size="small">
            <n-button size="small" @click="editCurrentGroup">{{ t('成员与工作区') }}</n-button>
            <n-button size="small" type="error" quaternary @click="deleteGroup">{{ t('解散') }}</n-button>
          </n-space>
          <n-space v-else size="small">
            <n-button v-if="currentGroup?.people?.some(p => p.id === auth.user?.id)" size="small" quaternary @click="leaveGroup">{{ t('退出群聊') }}</n-button>
            <n-button v-if="auth.user?.is_admin" size="small" type="error" quaternary @click="deleteGroup">{{ t('解散（管理员）') }}</n-button>
          </n-space>
        </template>
      </GroupChat>
      <div v-else-if="agentId" class="new-conversation">
        <h3>{{ t('和 {agent} 开始新对话', { agent: currentAgent?.name }) }}</h3>
        <p class="hint">
          {{ currentAgent?.address }} · {{ currentAgent?.hostname }}{{ currentAgent?.online ? '' : t('（离线，上线后自动执行）') }}
          <n-tag v-if="currentAgent" size="small" :bordered="false" :type="ISOLATION[currentAgent.isolation]?.type">{{ ISOLATION[currentAgent.isolation]?.label }}</n-tag>
        </p>
        <n-alert v-if="currentAgent && !isOwnAgent && !workspaceOptions.length" type="warning" :bordered="false" class="chat-alert">
          {{ t('{owner} 还没有给你授权任何工作区，暂时无法派任务。', { owner: currentAgent.owner_name }) }}
        </n-alert>
        <n-form label-placement="top">
          <n-form-item :label="t('工作区')">
            <n-select v-model:value="draft.workspace" :options="workspaceOptions" />
          </n-form-item>
          <n-form-item :label="t('任务内容')">
            <ImageAttach v-model="newImages">
              <n-input
                v-model:value="newPrompt"
                type="textarea"
                :autosize="{ minRows: 6, maxRows: 16 }"
                :placeholder="t('写清楚目标、步骤和期望产出。Ctrl/⌘+Enter 发送')"
                @keydown.ctrl.enter="startConversation"
                @keydown.meta.enter="startConversation"
              />
            </ImageAttach>
          </n-form-item>
        </n-form>
        <n-button type="primary" :loading="busy" :disabled="(!newPrompt.trim() && !newImages.length) || (!isOwnAgent && !workspaceOptions.length)" @click="startConversation"><template #icon><Send /></template>{{ t('发送') }}</n-button>
      </div>
      <div v-else class="empty-hint big">{{ t('从左侧选择好友、智能体或群聊开始对话') }}</div>
    </main>

    <n-modal v-model:show="showFriend" preset="card" :title="t('添加好友')" class="form-modal">
      <n-form label-placement="top">
        <n-form-item :label="t('同事')"><n-select v-model:value="friendForm.user" filterable :options="friendOptions" :placeholder="t('搜索用户名或姓名')" /></n-form-item>
        <n-form-item :label="t('附言')"><n-input v-model:value="friendForm.note" :placeholder="t('例如：我是运维的 alice')" /></n-form-item>
      </n-form>
      <template #footer><n-space justify="end"><n-button @click="showFriend = false">{{ t('取消') }}</n-button><n-button type="primary" :disabled="!friendForm.user" @click="addFriend">{{ t('发送申请') }}</n-button></n-space></template>
    </n-modal>

    <n-modal v-model:show="showAdd" preset="card" :title="t('申请使用好友的智能体')" class="form-modal">
      <n-form label-placement="top">
        <n-form-item :label="t('智能体')"><n-select v-model:value="addForm.agent" filterable :options="addOptions" :placeholder="t('只能选择好友的智能体')" /></n-form-item>
        <n-form-item :label="t('附言')"><n-input v-model:value="addForm.note" :placeholder="t('例如：想请你的 Claude 帮忙打包桌面客户端')" /></n-form-item>
      </n-form>
      <p v-if="!addOptions.length" class="hint">{{ t('没有可申请的智能体：请先在「好友」里添加同事，对方同意后再来申请。') }}</p>
      <template #footer><n-space justify="end"><n-button @click="showAdd = false">{{ t('取消') }}</n-button><n-button type="primary" :disabled="!addForm.agent" @click="addAccess">{{ t('发送申请') }}</n-button></n-space></template>
    </n-modal>

    <n-modal v-model:show="grant.show" preset="card" :title="grant.accept ? t('授权使用智能体') : t('调整工作区授权')" class="form-modal">
      <template v-if="grant.item">
        <p class="hint">
          <b>{{ grant.item.display_name || grant.item.username }}</b> {{ t('只能在你勾选的工作区里使用') }} <b>{{ grant.item.agent.name }}</b>{{ t('，TA 看到的只是工作区名称，看不到完整路径。') }}
          <n-tag size="small" :bordered="false" :type="ISOLATION[grant.item.agent.isolation]?.type">{{ ISOLATION[grant.item.agent.isolation]?.label }}</n-tag>
          {{ ISOLATION[grant.item.agent.isolation]?.tip }}
        </p>
        <n-checkbox-group v-model:value="grant.workspaces">
          <n-space vertical>
            <n-checkbox v-for="w in grant.item.agent.workspaces" :key="w.id" :value="w.id" :label="`${w.name} · ${w.path}`" />
          </n-space>
        </n-checkbox-group>
        <p v-if="!grant.item.agent.workspaces.length" class="hint">{{ t('该智能体还没有工作区，请先到「智能体」页添加。') }}</p>
        <n-checkbox v-if="grant.accept" v-model:checked="grant.trust" style="margin-top:12px">{{ t('同时对 TA 免审批（派来的任务直接执行）') }}</n-checkbox>
        <template v-if="grant.item.agent.isolation === 'none'">
          <n-checkbox v-model:checked="grant.allowFull" style="margin-top:12px">{{ t('允许 TA 使用这个完全权限的智能体') }}</n-checkbox>
          <n-alert v-if="grant.allowFull" type="warning" :show-icon="false" style="margin-top:8px">
            {{ t('TA 派来的任务会在你的电脑上不受隔离地执行：能读写任意文件、运行任何命令，也能用你本机的密钥和网络（例如 ssh 到你能登录的服务器）。') }}
            {{ t('是否需要你审批，仍按「免审批设置」决定。') }}
          </n-alert>
        </template>
      </template>
      <template #footer><n-space justify="end"><n-button @click="grant.show = false">{{ t('取消') }}</n-button><n-button type="primary" @click="saveGrant">{{ grant.accept ? t('通过') : t('保存') }}</n-button></n-space></template>
    </n-modal>

    <n-modal v-model:show="showGroup" preset="card" :title="groupForm.id ? t('编辑群聊') : t('新建群聊')" class="form-modal">
      <n-form label-placement="top">
        <n-form-item :label="t('群名')"><n-input v-model:value="groupForm.name" :placeholder="t('例如：v2.0 发版')" /></n-form-item>
        <n-form-item :label="t('好友（可选）')">
          <n-select v-model:value="groupForm.people" multiple filterable :options="peopleOptions" :placeholder="t('拉好友进群')" />
        </n-form-item>
        <n-form-item :label="t('智能体与工作区（可选，只能选自己的或已授权给你的）')">
          <div class="member-rows">
            <div v-for="(m, i) in groupForm.members" :key="i" class="member-row">
              <n-select v-model:value="m.agent" filterable :options="memberOptions" :placeholder="t('选择智能体')" @update:value="m.workspace = ''" />
              <n-select v-model:value="m.workspace" :options="memberWorkspaces(m.agent)" :disabled="!m.agent" />
              <n-button quaternary circle @click="groupForm.members.splice(i, 1)"><template #icon><Trash2 /></template></n-button>
            </div>
            <n-button dashed size="small" @click="groupForm.members.push({ agent: null, workspace: '' })"><template #icon><Plus /></template>{{ t('添加智能体') }}</n-button>
          </div>
        </n-form-item>
      </n-form>
      <template #footer><n-space justify="end"><n-button @click="showGroup = false">{{ t('取消') }}</n-button><n-button type="primary" @click="saveGroup">{{ t('保存') }}</n-button></n-space></template>
    </n-modal>
  </div>
</template>
