<script setup>
import { onMounted, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import PageHeader from '../components/PageHeader.vue'
import { api } from '../api/client'
import { useAuth } from '../composables/useAuth'
import { t } from '../i18n'
import { formatTime } from '../utils/format'

import ResponsiveTable from '../components/ResponsiveTable.vue'
const message = useMessage()
const { state: auth } = useAuth()
const logs = ref([])
const scope = ref('mine')
const ACTIONS = {
  'user.register': '注册', 'user.login': '登录', 'user.password': '修改密码',
  'task.create': '派发任务', 'task.approve': '批准任务', 'task.reject': '拒绝任务', 'task.cancel': '取消任务', 'task.follow_up': '追加对话',
  'artifact.review': '验收产物', 'workflow.start': '启动工作流', 'workflow.continue': '放行工作流步骤', 'workflow.cancel': '取消工作流', 'auto_reply.configure': '设置私聊自动回复', 'contact.allow_full': '设置完全权限使用许可',
  'token.create': '创建令牌', 'token.revoke': '吊销令牌', 'connector.online': '连接器上线', 'agent.delete': '移除智能体',
  'rule.create': '新增免审批', 'rule.delete': '删除免审批', 'rule.toggle': '切换免审批',
  'user.profile': '修改名称', 'admin.user': '管理用户',
  'work.connect': '连接工作服务器', 'work.repo.add': '工作区添加仓库', 'work.repo.branch': '修改仓库默认分支', 'work.session.create': '新建工作会话', 'work.push': '工作会话推送',
  'work.session.delete': '删除工作会话', 'work.settings': '修改额度切换设置', 'work.account.current': '切换当前账号', 'work.container': '维护工作容器',
  'work.gitlab.authorize': '授权 GitLab', 'work.mr.create': '创建合并请求',
  'work.server.agents': '设置开放给工作的智能体', 'work.server.access': '设置工作服务器使用范围',
  'collab.switch': '开关本机协作',
  'agent.visibility': '设置可见性', 'agent.workspaces': '修改工作区',
  'friend.request': '申请好友', 'friend.accept': '同意好友', 'friend.reject': '拒绝好友', 'friend.remove': '删除好友',
  'contact.request': '申请使用智能体', 'contact.accept': '授权智能体', 'contact.reject': '拒绝智能体授权', 'contact.remove': '收回智能体权限', 'contact.workspaces': '修改授权工作区',
  'group.create': '创建群聊', 'group.update': '修改群聊', 'group.delete': '解散群聊',
  'task.share': '分享对话', 'task.unshare': '取消分享', 'task.import_local': '导入本机会话',
  'release.publish': '发布连接器', 'release.withdraw': '撤回连接器',
}
const columns = [
  { title: t('时间'), key: 'created_at', width: 150, render: r => formatTime(r.created_at) },
  { title: t('用户'), key: 'username', width: 120 },
  { title: t('操作'), key: 'action', width: 130, render: r => (ACTIONS[r.action] ? t(ACTIONS[r.action]) : r.action) },
  { title: t('详情'), key: 'detail', render: r => r.detail },
]
async function load() {
  try { logs.value = (await api(`/api/audit?scope=${scope.value}`)).logs } catch (e) { message.error(e.message) }
}
watch(scope, load)
onMounted(load)
</script>

<template>
  <div class="page-wrap">
    <PageHeader :title="t('审计日志')" :description="t('登录、派发、审批、令牌与连接器等关键操作记录（最近 300 条）')">
      <template v-if="auth.user?.is_admin" #actions>
        <n-radio-group v-model:value="scope" size="small">
          <n-radio-button value="mine">{{ t('只看我的') }}</n-radio-button>
          <n-radio-button value="all">{{ t('全站') }}</n-radio-button>
        </n-radio-group>
      </template>
    </PageHeader>
    <n-card><ResponsiveTable :columns="columns" :data="logs" :row-key="r => r.id" /></n-card>
  </div>
</template>
