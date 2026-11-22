<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useMessage } from 'naive-ui'
import { Bot, KeyRound, Save } from '@lucide/vue'
import PageHeader from '../components/PageHeader.vue'
import { api } from '../api/client'
import { useAuth } from '../composables/useAuth'
import { t } from '../i18n'

const message = useMessage()
const { state: auth } = useAuth()

const name = ref(auth.user?.display_name || '')
const savingName = ref(false)
const nameChanged = computed(() => name.value.trim() && name.value.trim() !== auth.user?.display_name)

async function saveName() {
  savingName.value = true
  try {
    const result = await api('/api/auth/profile', { method: 'PUT', body: { display_name: name.value } })
    auth.user = { ...auth.user, ...result.user }
    name.value = result.user.display_name
    message.success(t('名称已更新'))
  } catch (e) {
    message.error(e.message)
  } finally {
    savingName.value = false
  }
}

const pw = reactive({ old: '', next: '', again: '' })
const savingPw = ref(false)
const pwProblem = computed(() => {
  if (!pw.old || !pw.next) return ''
  if (pw.next.length < 6) return t('新密码至少 6 位')
  if (pw.next === pw.old) return t('新密码不能和原密码相同')
  if (pw.again && pw.again !== pw.next) return t('两次输入的新密码不一致')
  return ''
})
const pwReady = computed(() => pw.old && pw.next && pw.again && !pwProblem.value)

async function savePassword() {
  savingPw.value = true
  try {
    await api('/api/auth/password', { method: 'POST', body: { old_password: pw.old, new_password: pw.next } })
    Object.assign(pw, { old: '', next: '', again: '' })
    message.success(t('密码已修改，下次登录请使用新密码'))
  } catch (e) {
    message.error(e.message)
  } finally {
    savingPw.value = false
  }
}

// ---- direct-message auto-reply: a default agent for every friend, overridden per friend ----
const myAgents = ref([])
const friends = ref([])
const auto = ref({ default: null, friends: [] })
const savingAuto = ref(false)
const usable = computed(() => myAgents.value.filter(a => ['sandbox', 'limited'].includes(a.isolation)))
const agentOptions = computed(() => usable.value.map(a => ({ label: `${a.name} (${a.address})${a.online ? '' : ' · ' + t('离线')}`, value: a.id })))
const wsOptions = id => (myAgents.value.find(a => a.id === id)?.workspaces || []).map(w => ({ label: w.name, value: w.id }))
const fullOnly = computed(() => myAgents.value.length && !usable.value.length)
const def = reactive({ agent_id: null, workspace: '' })
const overrideOf = id => auto.value.friends.find(f => f.friend_id === id)
const friendMode = id => { const o = overrideOf(id); return !o ? 'inherit' : o.agent_id ? 'agent' : 'off' }

async function loadAuto() {
  try {
    const [agents, fr, settings] = await Promise.all([api('/api/agents'), api('/api/friends'), api('/api/auto-replies')])
    myAgents.value = agents.agents.filter(a => a.access === 'owner')
    friends.value = fr.friends
    apply(settings)
  } catch (e) {
    message.error(e.message)
  }
}

function apply(settings) {
  auto.value = settings
  def.agent_id = settings.default?.agent_id ?? null
  def.workspace = settings.default?.workspace || ''
}

async function saveAuto(body) {
  savingAuto.value = true
  try {
    apply(await api('/api/auto-replies', { method: 'PUT', body }))
    message.success(t('自动回复设置已保存'))
  } catch (e) {
    message.error(e.message)
    loadAuto()
  } finally {
    savingAuto.value = false
  }
}

function saveDefault() {
  saveAuto({ friend_id: 0, agent_id: def.agent_id, workspace: def.agent_id ? def.workspace : '' })
}

function setFriend(f, mode, agentId, workspace) {
  if (mode === 'inherit') return saveAuto({ friend_id: f.user_id, inherit: true })
  if (mode === 'off') return saveAuto({ friend_id: f.user_id, agent_id: null })
  const id = agentId ?? overrideOf(f.user_id)?.agent_id ?? def.agent_id ?? usable.value[0]?.id
  if (!id) return message.warning(t('先接入一个沙箱隔离或受限的智能体'))
  saveAuto({ friend_id: f.user_id, agent_id: id, workspace: workspace ?? (agentId ? '' : overrideOf(f.user_id)?.workspace || '') })
}

onMounted(loadAuto)
</script>

<template>
  <div class="page-wrap profile-page">
    <PageHeader :title="t('我的')" :description="t('你的账号信息和登录密码。智能体是否对同事可见，在「智能体」列表的 ⋯ 菜单里设置')" />

    <div class="profile-grid">
      <n-card :title="t('个人信息')">
        <n-form label-placement="top" @submit.prevent>
          <n-form-item :label="t('用户名（登录和智能体地址用，不能修改）')">
            <n-input :value="auth.user?.username" disabled />
          </n-form-item>
          <n-form-item :label="t('显示名称（同事在好友、群聊里看到的名字）')">
            <n-input v-model:value="name" maxlength="40" show-count :placeholder="t('例如 王小明')" @keyup.enter="nameChanged && saveName()" />
          </n-form-item>
          <n-button type="primary" :disabled="!nameChanged" :loading="savingName" @click="saveName">
            <template #icon><Save /></template>{{ t('保存名称') }}
          </n-button>
        </n-form>
      </n-card>

      <n-card :title="t('修改密码')">
        <n-form label-placement="top" @submit.prevent>
          <n-form-item :label="t('原密码')">
            <n-input v-model:value="pw.old" type="password" show-password-on="click" autocomplete="current-password" />
          </n-form-item>
          <n-form-item :label="t('新密码（至少 6 位）')">
            <n-input v-model:value="pw.next" type="password" show-password-on="click" autocomplete="new-password" />
          </n-form-item>
          <n-form-item :label="t('再次输入新密码')" :feedback="pwProblem" :validation-status="pwProblem ? 'error' : undefined">
            <n-input v-model:value="pw.again" type="password" show-password-on="click" autocomplete="new-password" @keyup.enter="pwReady && savePassword()" />
          </n-form-item>
          <n-button type="primary" :disabled="!pwReady" :loading="savingPw" @click="savePassword">
            <template #icon><KeyRound /></template>{{ t('修改密码') }}
          </n-button>
        </n-form>
      </n-card>
    </div>

    <n-card :title="t('私聊自动回复')" class="auto-reply-card">
      <p class="auto-reply__hint">
        {{ t('好友给你发私聊时，由你选的智能体在指定工作区里执行并以你的名义回复，消息会标注“自动回复”。') }}
        {{ t('好友只看到回复内容，看不到执行过程；完全权限的智能体不能用于自动回复。') }}
      </p>
      <n-alert v-if="fullOnly" type="warning" :show-icon="false">{{ t('你的智能体都是完全权限，不能用于自动回复。') }}</n-alert>
      <n-alert v-if="auto.default?.problem" type="warning" :show-icon="false">{{ t('默认设置：{problem}', { problem: auto.default.problem }) }}</n-alert>
      <div class="auto-reply__row">
        <span class="auto-reply__label">{{ t('默认（所有好友）') }}</span>
        <n-select v-model:value="def.agent_id" :options="agentOptions" clearable :placeholder="t('不自动回复')" class="auto-reply__agent"
                  @update:value="def.workspace = ''" />
        <n-select v-model:value="def.workspace" :options="wsOptions(def.agent_id)" :disabled="!def.agent_id" :placeholder="t('工作区')"
                  class="auto-reply__ws" />
        <n-button type="primary" :loading="savingAuto" @click="saveDefault"><template #icon><Bot /></template>{{ t('保存') }}</n-button>
      </div>
      <div v-if="friends.length" class="auto-reply__friends">
        <div v-for="f in friends" :key="f.user_id" class="auto-reply__row">
          <span class="auto-reply__label">{{ f.display_name || f.username }} <small>@{{ f.username }}</small></span>
          <n-select :value="friendMode(f.user_id)" class="auto-reply__mode"
                    :options="[{ label: t('沿用默认'), value: 'inherit' }, { label: t('不自动回复'), value: 'off' }, { label: t('指定智能体'), value: 'agent' }]"
                    @update:value="m => setFriend(f, m)" />
          <template v-if="friendMode(f.user_id) === 'agent'">
            <n-select :value="overrideOf(f.user_id).agent_id" :options="agentOptions" class="auto-reply__agent"
                      @update:value="id => setFriend(f, 'agent', id, '')" />
            <n-select :value="overrideOf(f.user_id).workspace || null" :options="wsOptions(overrideOf(f.user_id).agent_id)"
                      :placeholder="t('工作区')" class="auto-reply__ws"
                      @update:value="w => setFriend(f, 'agent', overrideOf(f.user_id).agent_id, w)" />
          </template>
          <small v-if="overrideOf(f.user_id)?.problem" class="auto-reply__problem">{{ overrideOf(f.user_id).problem }}</small>
        </div>
      </div>
      <div v-else class="empty-hint">{{ t('还没有好友') }}</div>
    </n-card>
  </div>
</template>
