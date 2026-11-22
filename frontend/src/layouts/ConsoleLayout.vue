<script setup>
import { computed, h, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Bot,
  Briefcase,
  CheckCheck,
  Hourglass,
  Languages,
  History,
  LayoutDashboard,
  ListTodo,
  MessagesSquare,
  LogOut,
  Moon,
  PackageOpen,
  PanelLeftClose,
  PanelLeftOpen,
  PlugZap,
  Sun,
  UserRound,
  UsersRound,
  Workflow,
  Search,
  Server,
  Monitor,
  MonitorCog,
  Network,
} from '@lucide/vue'
import { NBadge, NIcon } from 'naive-ui'
import { api } from '../api/client'
import { useAuth } from '../composables/useAuth'
import { useTheme } from '../composables/useTheme'
import { LOCALES, locale, setLocale, t } from '../i18n'
import BrandMark from '../components/BrandMark.vue'
import CommandPalette from '../components/CommandPalette.vue'

const route = useRoute()
const router = useRouter()
const collapsed = ref(window.matchMedia('(max-width: 900px)').matches)
const pending = ref(0)
const { state: authState, logout } = useAuth()
const { isDark, choice, setChoice } = useTheme()

const renderIcon = component => () => h(NIcon, null, { default: () => h(component) })
const themeOptions = [
  { key: 'light', label: t('明亮'), icon: renderIcon(Sun) },
  { key: 'dark', label: t('暗黑'), icon: renderIcon(Moon) },
  { key: 'system', label: t('跟随系统'), icon: renderIcon(MonitorCog) },
]
const item = (label, key, icon) => ({ label: t(label), key, icon: renderIcon(icon) })
const menuOptions = computed(() => [
  {
    type: 'group', label: t('协作'), key: 'g-work', children: [
      item('协作概览', 'overview', LayoutDashboard),
      item('对话', 'chat', MessagesSquare),
      item('智能体', 'agents', Bot),
      {
        label: () => h('span', { class: 'menu-with-badge' }, [
          t('任务中心'),
          pending.value ? h(NBadge, { value: pending.value, max: 99, type: 'warning' }) : null,
        ]),
        key: 'tasks',
        icon: renderIcon(ListTodo),
      },
      item('工作流', 'workflows', Workflow),
      item('工作', 'work', Briefcase),
      item('本机协作', 'collab', Network),
    ],
  },
  {
    type: 'group', label: t('接入与规则'), key: 'g-setup', children: [
      item('接入配置', 'connect', PlugZap),
      item('服务器维护', 'work-admin', Server),
      item('免审批设置', 'rules', CheckCheck),
      item(authState.user?.is_admin ? '连接器版本' : '下载连接器', 'releases', PackageOpen),
      item('审计日志', 'audit', History),
      item('我的', 'profile', UserRound),
    ],
  },
  ...(authState.user?.is_admin
    ? [{ type: 'group', label: t('管理'), key: 'g-admin', children: [item('主机管理', 'hosts', Monitor), item('用户管理', 'users', UsersRound)] }]
    : []),
])
const activeMenu = computed(() => (route.name === 'task-detail' ? 'tasks' : String(route.name || 'overview')))
const palette = ref(null)
const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)
const initial = computed(() => (authState.user?.display_name || authState.user?.username || '?').slice(0, 1).toUpperCase())

async function refreshPending() {
  try {
    const result = await api('/api/tasks?box=approval&limit=100')
    pending.value = result.tasks.length
  } catch { /* ignore */ }
}

// fold the menu when the window becomes narrow, unfold when it grows back
const narrow = window.matchMedia('(max-width: 900px)')
const onNarrow = e => { collapsed.value = e.matches }

let timer = null
onMounted(() => {
  refreshPending()
  timer = setInterval(refreshPending, 10000)
  narrow.addEventListener('change', onNarrow)
})
onBeforeUnmount(() => {
  clearInterval(timer)
  narrow.removeEventListener('change', onNarrow)
})

function signOut() {
  logout()
  router.replace({ name: 'login' })
}
</script>

<template>
  <n-layout class="app-shell" has-sider>
    <n-layout-sider
      collapse-mode="width"
      :collapsed-width="64"
      :width="232"
      :collapsed="collapsed"
      class="app-sidebar"
    >
      <div class="brand" :class="{ collapsed }">
        <BrandMark :size="34" />
        <div v-if="!collapsed" class="brand__text">
          <strong>Agent Hub</strong>
          <small>{{ t('团队智能体协作中心') }}</small>
        </div>
      </div>
      <n-menu
        :value="activeMenu"
        :options="menuOptions"
        :collapsed="collapsed"
        :collapsed-width="64"
        :collapsed-icon-size="20"
        :indent="16"
        @update:value="key => router.push({ name: key })"
      />
      <div class="rail-foot" :class="{ collapsed }">
        <button class="rail-btn" :aria-label="collapsed ? t('展开菜单') : t('收起菜单')" @click="collapsed = !collapsed">
          <PanelLeftOpen v-if="collapsed" :size="18" /><PanelLeftClose v-else :size="18" />
          <span v-if="!collapsed">{{ t('收起菜单') }}</span>
        </button>
      </div>
    </n-layout-sider>

    <n-layout class="app-main">
      <header class="app-header">
        <button v-if="pending" class="pending-pill" @click="router.push({ name: 'tasks', query: { box: 'approval' } })">
          <Hourglass :size="14" />{{ pending }}<span class="pending-pill__text">{{ t(' 个任务等你审批') }}</span>
        </button>
        <button class="palette-trigger" :aria-label="t('打开命令面板')" @click="palette?.show()">
          <Search :size="15" /><span class="palette-trigger__text">{{ t('搜索或跳转') }}</span><kbd>{{ isMac ? '⌘' : 'Ctrl' }} K</kbd>
        </button>
        <div class="header-actions">
          <n-dropdown trigger="click" :options="themeOptions.map(o => ({ ...o, label: o.label + (choice === o.key ? '  ✓' : '') }))" @select="setChoice">
            <button class="icon-btn" :aria-label="t('切换主题')" :title="t('切换主题')">
              <MonitorCog v-if="choice === 'system'" :size="18" /><Moon v-else-if="isDark" :size="18" /><Sun v-else :size="18" />
            </button>
          </n-dropdown>
          <n-dropdown trigger="click" :options="LOCALES.map(l => ({ key: l.key, label: l.label + (locale === l.key ? '  ✓' : '') }))" @select="setLocale">
            <button class="icon-btn" aria-label="Language / 语言" title="Language / 语言"><Languages :size="18" /></button>
          </n-dropdown>
          <button class="header-user" :class="{ active: route.name === 'profile' }" :title="t('我的：名称和密码')" @click="router.push({ name: 'profile' })">
            <span class="header-user__avatar">{{ initial }}</span>
            <span class="header-user__name">{{ authState.user?.display_name || authState.user?.username }}</span>
            <n-tag v-if="authState.user?.is_admin" size="tiny" type="info" :bordered="false">{{ t('管理员') }}</n-tag>
          </button>
          <n-tooltip>
            <template #trigger>
              <button class="icon-btn" :aria-label="t('退出登录')" @click="signOut"><LogOut :size="18" /></button>
            </template>
            {{ t('退出登录') }}
          </n-tooltip>
        </div>
      </header>

      <n-layout-content class="app-content" :native-scrollbar="true">
        <router-view @changed="refreshPending" />
      </n-layout-content>
      <CommandPalette ref="palette" />
    </n-layout>
  </n-layout>
</template>
