<script setup>
import { h, onMounted, ref } from 'vue'
import { NButton, NSpace, NTag, useMessage } from 'naive-ui'
import PageHeader from '../components/PageHeader.vue'
import { api } from '../api/client'
import { useAuth } from '../composables/useAuth'
import { t } from '../i18n'
import { formatTime } from '../utils/format'

import ResponsiveTable from '../components/ResponsiveTable.vue'
const message = useMessage()
const { state: auth } = useAuth()
const users = ref([])

async function load() {
  try { users.value = (await api('/api/admin/users')).users } catch (e) { message.error(e.message) }
}

async function update(row, body) {
  try {
    await api(`/api/admin/users/${row.id}`, { method: 'PUT', body })
    load()
  } catch (e) { message.error(e.message) }
}

const columns = [
  { title: t('用户名'), key: 'username', width: 140 },
  { title: t('显示名称'), key: 'display_name', width: 140 },
  {
    title: t('角色'), key: 'is_admin', width: 100,
    render: r => h(NTag, { size: 'small', bordered: false, type: r.is_admin ? 'warning' : 'default' }, () => (r.is_admin ? t('管理员') : t('普通用户'))),
  },
  {
    title: t('状态'), key: 'disabled', width: 90,
    render: r => h(NTag, { size: 'small', bordered: false, type: r.disabled ? 'error' : 'success' }, () => (r.disabled ? t('已停用') : t('正常'))),
  },
  { title: t('智能体'), key: 'agents', width: 80 },
  { title: t('在线连接器'), key: 'connectors_online', width: 100 },
  { title: t('发起任务'), key: 'tasks_sent', width: 90 },
  { title: t('注册时间'), key: 'created_at', width: 150, render: r => formatTime(r.created_at) },
  { title: t('最近登录'), key: 'last_login', width: 150, render: r => (r.last_login ? formatTime(r.last_login) : '—') },
  {
    title: t('操作'), key: 'actions', width: 210, fixed: 'right',
    render: r => r.id === auth.user?.id ? t('当前账号') : h(NSpace, { size: 'small' }, () => [
      h(NButton, { size: 'tiny', onClick: () => update(r, { is_admin: !r.is_admin }) }, () => (r.is_admin ? t('设为普通用户') : t('设为管理员'))),
      h(NButton, { size: 'tiny', type: r.disabled ? 'primary' : 'error', secondary: true, onClick: () => update(r, { disabled: !r.disabled }) },
        () => (r.disabled ? t('启用') : t('停用'))),
    ]),
  },
]

onMounted(load)
</script>

<template>
  <div class="page-wrap">
    <PageHeader :title="t('用户管理')" :description="t('管理员可以调整角色、停用账号；全站任务、群聊和审计日志也对管理员可见')" />
    <n-card><ResponsiveTable :columns="columns" :data="users" :row-key="r => r.id" :scroll-x="1360" /></n-card>
  </div>
</template>
