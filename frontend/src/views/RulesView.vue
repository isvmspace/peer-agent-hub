<script setup>
import { computed, h, onMounted, reactive, ref } from 'vue'
import { NButton, NSwitch, useMessage } from 'naive-ui'
import { Plus } from '@lucide/vue'
import PageHeader from '../components/PageHeader.vue'
import { api } from '../api/client'
import { useAuth } from '../composables/useAuth'
import { t } from '../i18n'
import { formatTime } from '../utils/format'

import ResponsiveTable from '../components/ResponsiveTable.vue'
const message = useMessage()
const { state: auth } = useAuth()
const rules = ref([])
const users = ref([])
const agents = ref([])
const form = reactive({ from_user_id: null, agent_id: null, note: '' })

const userOptions = computed(() => [
  { label: t('任何人'), value: 0 },
  ...users.value.filter(u => u.id !== auth.user?.id).map(u => ({ label: `${u.display_name}（${u.username}）`, value: u.id })),
])
const agentOptions = computed(() => [
  { label: t('我的全部智能体'), value: 0 },
  ...agents.value.filter(a => a.user_id === auth.user?.id).map(a => ({ label: `${a.name}（${a.address}）`, value: a.id })),
])

async function load() {
  try {
    const [r, u, a] = await Promise.all([api('/api/rules'), api('/api/users'), api('/api/agents')])
    rules.value = r.rules
    users.value = u.users
    agents.value = a.agents
  } catch (e) {
    message.error(e.message)
  }
}

async function add() {
  if (form.from_user_id === null) {
    message.warning(t('请选择对谁免审批'))
    return
  }
  try {
    await api('/api/rules', { method: 'POST', body: { from_user_id: form.from_user_id || null, agent_id: form.agent_id || null, note: form.note } })
    message.success(t('已添加'))
    Object.assign(form, { from_user_id: null, agent_id: null, note: '' })
    load()
  } catch (e) {
    message.error(e.message)
  }
}

async function toggle(row, enabled) {
  try { await api(`/api/rules/${row.id}`, { method: 'PUT', body: { enabled } }); row.enabled = enabled ? 1 : 0 } catch (e) { message.error(e.message) }
}

async function remove(row) {
  try { await api(`/api/rules/${row.id}`, { method: 'DELETE' }); load() } catch (e) { message.error(e.message) }
}

const columns = [
  { title: t('发起人'), key: 'from', render: r => (r.from_user_id ? `${r.from_display_name}（${r.from_username}）` : t('任何人')) },
  { title: t('我的智能体'), key: 'agent', render: r => (r.agent_id ? `${r.agent_name}（${r.agent_key}）` : t('全部')) },
  { title: t('备注'), key: 'note' },
  { title: t('创建'), key: 'created_at', width: 140, render: r => formatTime(r.created_at) },
  { title: t('启用'), key: 'enabled', width: 80, render: r => h(NSwitch, { value: !!r.enabled, size: 'small', onUpdateValue: v => toggle(r, v) }) },
  { title: '', key: 'a', width: 70, render: r => h(NButton, { size: 'small', quaternary: true, type: 'error', onClick: () => remove(r) }, () => t('删除')) },
]

onMounted(load)
</script>

<template>
  <div class="page-wrap">
    <PageHeader :title="t('免审批设置')" :description="t('默认别人派给你智能体的任务需要你本人批准；匹配下列任一规则的任务会直接执行。自己派给自己的任务始终免审批。')" />
    <n-card :title="t('添加规则')">
      <n-space align="center" :wrap="true">
        <n-select v-model:value="form.from_user_id" :options="userOptions" :placeholder="t('对谁免审批')" filterable style="width: 240px" />
        <n-select v-model:value="form.agent_id" :options="agentOptions" :placeholder="t('哪个智能体（默认全部）')" clearable style="width: 260px" />
        <n-input v-model:value="form.note" :placeholder="t('备注（可选）')" style="width: 220px" />
        <n-button type="primary" @click="add"><template #icon><Plus /></template>{{ t('添加') }}</n-button>
      </n-space>
      <n-alert v-if="form.from_user_id === 0" type="warning" class="token-alert" :title="t('注意')">{{ t('「任何人」意味着站点上所有用户都能直接让你的智能体以完整权限在你电脑上执行命令。') }}</n-alert>
    </n-card>
    <n-card :title="t('我的规则')">
      <ResponsiveTable :columns="columns" :data="rules" :row-key="r => r.id" />
    </n-card>
  </div>
</template>
