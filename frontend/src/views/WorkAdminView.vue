<script setup>
// 服务器维护：把自己的电脑注册成工作服务器（开放哪些智能体、谁能用），以及工作服务器的资源、
// 智能体账号的订阅与额度、额度自动切换、同事用量和容器。归属人维护自己的机器，管理员能维护全部。
import { computed, onMounted, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import { RefreshCw } from '@lucide/vue'
import PageHeader from '../components/PageHeader.vue'
import { api } from '../api/client'
import { formatTime } from '../utils/format'
import { useAuth } from '../composables/useAuth'
import { t } from '../i18n'

const { state: authState } = useAuth()
const authStateIsAdmin = computed(() => !!authState.user?.is_admin)

const message = useMessage()
const servers = ref([])
const hosts = ref([])
const people = ref([])
const hostForm = ref({})
function resetHostForms() {
  hostForm.value = Object.fromEntries(hosts.value.map(h => [h.id, {
    keys: h.agents.filter(a => a.workhost).map(a => a.key), access: h.access, users: h.allowed.map(u => u.username),
  }]))
}
async function saveAgents(h) {
  try {
    await api(`/api/work/hosts/${h.id}/agents`, { method: 'POST', body: { keys: hostForm.value[h.id].keys } })
    message.success(t('已发给连接器，几秒后生效'))
    setTimeout(loadServers, 1500)
  } catch (e) {
    message.error(e.message)
  }
}
async function saveAccess(h) {
  try {
    const f = hostForm.value[h.id]
    await api(`/api/work/hosts/${h.id}/access`, { method: 'POST', body: { access: f.access, users: f.access === 'list' ? f.users : [] } })
    message.success(t('已保存'))
    await loadServers()
  } catch (e) {
    message.error(e.message)
  }
}
const peopleOptions = computed(() => people.value.map(u => ({ label: `${u.display_name || u.username} (${u.username})`, value: u.username })))
const serverId = ref(null)
const data = ref(null)
const loading = ref(false)
const kind = ref('codex')
const period = ref('today')
const settings = ref({ auto_switch: true, threshold: 20, on_limit: 'auto' })

async function loadServers() {
  try {
    const r = await api('/api/work/admin')
    servers.value = r.servers
    hosts.value = r.hosts
    resetHostForms()
    if (!serverId.value) serverId.value = servers.value.find(s => s.online)?.id ?? servers.value[0]?.id ?? null
  } catch (e) {
    message.error(e.message)
  }
}
async function load() {
  if (!serverId.value) return
  loading.value = true
  try {
    data.value = await api(`/api/work/admin?connector_id=${serverId.value}`)
    const s = data.value.settings
    settings.value = { auto_switch: s.auto_switch, threshold: s.threshold, on_limit: s.on_limit }
    if (!data.value.accounts.some(a => a.kind === kind.value) && data.value.accounts.length) kind.value = data.value.accounts[0].kind
  } catch (e) {
    message.error(e.message)
  } finally {
    loading.value = false
  }
}
watch(serverId, load)

const gb = bytes => (bytes ? `${(bytes / 1024 ** 3).toFixed(0)} GB` : '—')
const kinds = computed(() => [...new Set((data.value?.accounts || []).map(a => a.kind))])
const accounts = computed(() => (data.value?.accounts || []).filter(a => a.kind === kind.value))
const agentName = k => data.value?.agents.find(a => a.kind === k)?.name || k

function windowLabel(w) {
  if (!w) return ''
  const m = w.window_minutes
  return m >= 1440 * 6 ? t('本周') : m >= 60 ? t('{h} 小时窗口', { h: Math.round(m / 60) }) : t('{m} 分钟窗口', { m })
}
function resetText(ts) {
  if (!ts) return ''
  const s = ts - Date.now() / 1000
  if (s <= 0) return t('即将重置')
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60)
  const parts = `${d ? t('{n} 天', { n: d }) + ' ' : ''}${h ? t('{n} 小时', { n: h }) + ' ' : ''}${d ? '' : t('{n} 分', { n: m })}`.trim()
  return t('{time}后重置', { time: parts })
}
const left = w => Math.max(0, Math.round(100 - (w?.used_percent ?? 0)))
const barColor = l => (l < 20 ? '#d03050' : l < 40 ? '#f0a020' : '#18a058')

async function saveSettings() {
  try {
    await api(`/api/work/admin/${serverId.value}/settings`, { method: 'POST', body: settings.value })
    message.success(t('已保存'))
    await load()
  } catch (e) {
    message.error(e.message)
  }
}
async function setCurrent(a) {
  try {
    await api(`/api/work/admin/${serverId.value}/current`, { method: 'POST', body: { account_id: a.account_id } })
    await load()
  } catch (e) {
    message.error(e.message)
  }
}
const acting = ref('')
async function act(c, action) {
  acting.value = c.user + action
  try {
    await api(`/api/work/admin/${serverId.value}/containers/${c.user}/${action}`, { method: 'POST' })
    message.success({ stop: t('已休眠'), start: t('已启动'), restart: t('已重启') }[action])
    await load()
  } catch (e) {
    message.error(e.message)
  } finally {
    acting.value = ''
  }
}
const usage = computed(() => {
  const rows = data.value?.usage || []
  const max = Math.max(1, ...rows.map(r => r[period.value]))
  return rows.map(r => ({ ...r, value: r[period.value], pct: Math.round((r[period.value] / max) * 100) }))
})
const tokens = n => (n >= 1e6 ? `${(n / 1e6).toFixed(2)} M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)} K` : String(n))
const STATE = { running: [t('运行中'), 'success'], exited: [t('已休眠'), 'default'], created: [t('已创建'), 'default'] }

onMounted(async () => {
  api('/api/users').then(r => { people.value = r.users }).catch(() => {})
  await loadServers()
  await load()
})
</script>

<template>
  <div>
    <PageHeader :title="t('服务器维护')" :description="t('把自己的电脑注册成工作服务器给同事用；维护工作服务器上的智能体账号、额度和同事的容器。')">
      <template #actions>
        <n-select v-model:value="serverId" :options="servers.map(s => ({ label: s.name + (s.online ? '' : ' ' + t('（离线）')), value: s.id }))" style="width: 220px" />
        <n-button :loading="loading" @click="load"><template #icon><RefreshCw /></template>{{ t('刷新') }}</n-button>
      </template>
    </PageHeader>
    <n-card :title="authStateIsAdmin ? t('机器（全部）') : t('我的机器')" class="block">
      <n-empty v-if="!hosts.length" :description="t('还没有接入的机器：先在「接入配置」里安装并运行连接器，机器就会出现在这里。')" />
      <div v-for="h in hosts" :key="h.id" class="host">
        <div class="host-head">
          <div>
            <strong>{{ h.name }}</strong>
            <n-text depth="3"> · {{ h.os }}{{ h.mine ? '' : ' · ' + h.owner }}</n-text>
          </div>
          <n-space size="small">
            <n-tag size="small" :type="h.online ? 'success' : 'default'" :bordered="false">{{ h.online ? t('在线') : t('离线') }}</n-tag>
            <n-tag size="small" :type="h.workhost ? 'success' : 'default'" :bordered="false">{{ h.workhost ? t('工作服务器 · {n} 位同事', { n: h.colleagues }) : t('未开启工作服务器') }}</n-tag>
          </n-space>
        </div>
        <template v-if="!h.workhost">
          <ol class="steps">
            <li>{{ t('安装 Docker 并启动') }}{{ h.docker ? t('（已检测到 ✓）') : '' }}{{ t('。') }}</li>
            <li>{{ t('构建同事容器用的镜像（在 ai-agent-hub 代码目录）：') }}<code>docker build -t agent-hub-work:latest deploy/workhost</code></li>
            <li>{{ t('在这台机器上开启工作服务器，') }}<code>--agents</code>{{ t(' 写要开放的智能体：') }}
              <code>agent-hub-connector workhost enable --agents {{ h.agents.map(a => a.key).join(',') }} --cpus 4 --memory 8g</code></li>
            <li>{{ t('重启连接器（') }}<code>agent-hub-connector install</code>{{ t(' 或重新 ') }}<code>run</code>{{ t('），回到这里选择开放的智能体和使用范围。') }}</li>
          </ol>
          <n-text depth="3" class="small">{{ t('开放的智能体用这台机器上它们自己的登录账号，同事的会话会用掉这些账号的额度；同事的代码在各自独立的容器里，碰不到你的文件。') }}</n-text>
        </template>
        <div v-else-if="hostForm[h.id]" class="host-grid">
          <div>
            <div class="small label">{{ t('开放给「工作」的智能体') }}</div>
            <n-checkbox-group v-model:value="hostForm[h.id].keys">
              <n-space vertical>
                <n-checkbox v-for="a in h.agents" :key="a.key" :value="a.key">{{ a.name }}<n-text depth="3"> ({{ a.key }})</n-text></n-checkbox>
              </n-space>
            </n-checkbox-group>
            <n-text depth="3" class="small block-text">{{ t('没勾选的智能体照常作为你自己的智能体使用。') }}</n-text>
            <n-button size="small" type="primary" secondary :disabled="!h.online" @click="saveAgents(h)">{{ t('保存') }}</n-button>
          </div>
          <div>
            <div class="small label">{{ t('谁能使用') }}</div>
            <n-radio-group v-model:value="hostForm[h.id].access">
              <n-space vertical>
                <n-radio value="all">{{ t('所有同事') }}</n-radio>
                <n-radio value="list">{{ t('只有指定的人') }}</n-radio>
              </n-space>
            </n-radio-group>
            <n-select v-if="hostForm[h.id].access === 'list'" v-model:value="hostForm[h.id].users" multiple filterable :options="peopleOptions" :placeholder="t('选择同事')" style="margin-top: 8px" />
            <div style="margin-top: 8px"><n-button size="small" type="primary" secondary @click="saveAccess(h)">{{ t('保存') }}</n-button></div>
          </div>
        </div>
      </div>
    </n-card>

    <n-empty v-if="!servers.length" :description="t('开启工作服务器并开放智能体后，这里会显示它的账号额度、同事用量和容器。')" />
    <n-spin v-else :show="loading && !data">
      <template v-if="data">
        <div class="tiles">
          <n-card size="small"><div class="tile-label">CPU</div><div class="tile-value">{{ t('{n} 核', { n: data.host.cpus || '—' }) }}</div><div class="tile-sub">{{ t('每人上限 {v}', { v: data.host.cpus_per_user || t('不限') }) }}</div></n-card>
          <n-card size="small"><div class="tile-label">{{ t('内存') }}</div><div class="tile-value">{{ gb(data.host.memory_bytes) }}</div><div class="tile-sub">{{ t('每人上限 {v}', { v: data.host.memory_per_user || t('不限') }) }}</div></n-card>
          <n-card size="small"><div class="tile-label">{{ t('容器') }}</div><div class="tile-value">{{ t('{n} 运行 / {total}', { n: data.containers.filter(c => c.state === 'running').length, total: data.containers.length }) }}</div><div class="tile-sub">{{ t('闲置 {n} 分钟自动休眠', { n: data.host.idle_minutes }) }}</div></n-card>
          <n-card size="small"><div class="tile-label">{{ t('运行中会话') }}</div><div class="tile-value">{{ data.host.running_sessions }}</div><div class="tile-sub">{{ t('每人最多 {n} 个同时运行', { n: data.host.max_sessions_per_user || 3 }) }}</div></n-card>
        </div>

        <n-card :title="t('智能体账号')" class="block">
          <template #header-extra>
            <n-radio-group v-if="kinds.length > 1" v-model:value="kind" size="small">
              <n-radio-button v-for="k in kinds" :key="k" :value="k">{{ agentName(k) }}</n-radio-button>
            </n-radio-group>
          </template>
          <n-empty v-if="!accounts.length" :description="t('这台服务器没有配置账号（连接器 workhost.accounts）')" />
          <div class="accounts">
            <div v-for="a in accounts" :key="a.account_id" :class="['account', { current: a.current }]">
              <div class="account-head">
                <div>
                  <div class="account-name">{{ a.account_id }}</div>
                  <n-space size="small" align="center">
                    <n-tag v-if="a.current" size="small" type="success" round>{{ t('当前使用') }}</n-tag>
                    <n-tag size="small" round :bordered="false">{{ (a.plan || '—').toUpperCase() }}</n-tag>
                    <n-text depth="3" style="font-size: 12px">{{ a.email }}</n-text>
                  </n-space>
                </div>
                <n-button v-if="!a.current" size="small" secondary type="primary" @click="setCurrent(a)">{{ t('设为当前') }}</n-button>
              </div>
              <n-alert v-if="a.quota.error" type="warning" :show-icon="false">{{ t('读取额度失败：{error}', { error: a.quota.error }) }}</n-alert>
              <template v-for="w in [a.quota.primary, a.quota.secondary]" :key="w ? w.window_minutes : Math.random()">
                <div v-if="w" class="quota">
                  <div class="quota-head"><span>{{ windowLabel(w) }}</span><span class="dim">{{ resetText(w.resets_at) }}</span></div>
                  <n-progress type="line" :percentage="left(w)" :color="barColor(left(w))" :show-indicator="false" :height="8" />
                  <div class="quota-val" :style="{ color: barColor(left(w)) }">{{ t('剩余 {n}%', { n: left(w) }) }}</div>
                </div>
              </template>
              <div class="dim small">{{ a.exhausted ? t('额度已用完，暂不分配新会话') : t('可用') }} · {{ t('更新于 {time}', { time: formatTime(a.updated_at) }) }}</div>
            </div>
          </div>

          <n-divider />
          <div class="switch-grid">
            <div>
              <n-checkbox v-model:checked="settings.auto_switch"><strong>{{ t('额度不足时自动切换账号') }}</strong></n-checkbox>
              <p class="dim small">{{ t('按轮切换，不暂停对话：当前账号剩余低于阈值时，新的一轮改用额度最多的账号；正在执行的一轮用原账号跑完，同一会话续聊，上下文不丢。') }}</p>
              <n-space align="center"><span class="small">{{ t('阈值：剩余低于') }}</span><n-input-number v-model:value="settings.threshold" :min="0" :max="90" size="small" style="width: 100px" /><span class="small">%</span></n-space>
              <p class="small" style="margin: 12px 0 6px">{{ t('一轮执行到一半、原账号额度用完时：') }}</p>
              <n-radio-group v-model:value="settings.on_limit">
                <n-space vertical>
                  <n-radio value="auto">{{ t('自动续聊（推荐）：用新账号补发「继续刚才的任务」') }}</n-radio>
                  <n-radio value="confirm">{{ t('等用户确认：提示已切换，同事点继续后接着做') }}</n-radio>
                </n-space>
              </n-radio-group>
              <div style="margin-top: 12px"><n-button type="primary" size="small" @click="saveSettings">{{ t('保存设置') }}</n-button></div>
            </div>
            <div>
              <div class="small" style="font-weight: 600; margin-bottom: 8px">{{ t('切换记录') }}</div>
              <n-empty v-if="!data.switches.length" size="small" :description="t('暂无')" />
              <div v-for="s in data.switches" :key="s.id" class="switch-row">
                <span class="dim">{{ formatTime(s.created_at) }}</span>
                <span><strong>{{ s.from_account || '—' }} → {{ s.to_account }}</strong><span class="dim"> · {{ s.reason }}</span></span>
              </div>
            </div>
          </div>
        </n-card>

        <n-card :title="t('Token 用量 · 按同事')" class="block">
          <template #header-extra>
            <n-radio-group v-model:value="period" size="small">
              <n-radio-button value="today">{{ t('今日') }}</n-radio-button><n-radio-button value="week">{{ t('近 7 天') }}</n-radio-button><n-radio-button value="total">{{ t('累计') }}</n-radio-button>
            </n-radio-group>
          </template>
          <n-empty v-if="!usage.length" :description="t('还没有用量')" />
          <div v-for="u in usage" :key="u.user" class="usage-row">
            <span>{{ u.user }}</span>
            <n-progress type="line" :percentage="u.pct" :show-indicator="false" :height="12" color="#4f8a6b" />
            <span class="num">{{ tokens(u.value) }}</span>
          </div>
        </n-card>

        <n-card :title="t('同事容器 · {n} 个', { n: data.containers.length })" class="block">
          <n-table size="small" :bordered="false" :single-line="false">
            <thead><tr><th>{{ t('同事') }}</th><th>{{ t('状态') }}</th><th>{{ t('运行中会话') }}</th><th>CPU</th><th>{{ t('内存') }}</th><th>{{ t('磁盘') }}</th><th>{{ t('最近活动') }}</th><th>{{ t('操作') }}</th></tr></thead>
            <tbody>
              <tr v-for="c in data.containers" :key="c.name">
                <td><strong>{{ c.username }}</strong></td>
                <td><n-tag size="small" :type="(STATE[c.state] || [c.state, 'warning'])[1]" :bordered="false">{{ (STATE[c.state] || [c.state])[0] }}</n-tag></td>
                <td>{{ c.running_turns }}</td>
                <td>{{ c.cpu || '—' }}</td>
                <td>{{ c.memory || '—' }}</td>
                <td>{{ c.disk || '—' }}</td>
                <td>{{ c.last_active ? formatTime(c.last_active) : '—' }}</td>
                <td>
                  <n-space size="small">
                    <n-button v-if="c.state === 'running'" size="tiny" :loading="acting === c.user + 'stop'" @click="act(c, 'stop')">{{ t('休眠') }}</n-button>
                    <n-button v-else size="tiny" :loading="acting === c.user + 'start'" @click="act(c, 'start')">{{ t('启动') }}</n-button>
                    <n-button size="tiny" :loading="acting === c.user + 'restart'" @click="act(c, 'restart')">{{ t('重启') }}</n-button>
                  </n-space>
                </td>
              </tr>
            </tbody>
          </n-table>
        </n-card>
      </template>
    </n-spin>
  </div>
</template>

<style scoped>
.host { border: 1px solid rgba(128,128,128,.22); border-radius: 10px; padding: 12px 14px; margin-bottom: 10px; }
.host-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.host-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 24px; }
.steps { margin: 4px 0 8px; padding-left: 20px; font-size: 13px; line-height: 1.9; }
.steps code { font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 12px; background: rgba(128,128,128,.12); padding: 1px 6px; border-radius: 4px; word-break: break-all; }
.label { font-weight: 600; margin-bottom: 6px; }
.block-text { display: block; margin: 6px 0; }
.tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px; }
.tile-label, .tile-sub, .dim { opacity: .65; font-size: 12px; }
.tile-value { font-size: 20px; font-weight: 650; margin: 4px 0; font-variant-numeric: tabular-nums; }
.block { margin-bottom: 16px; }
.accounts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.account { border: 1px solid rgba(128,128,128,.25); border-radius: 12px; padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; }
.account.current { border: 1.5px solid #18a058; background: rgba(24,160,88,.05); }
.account-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }
.account-name { font-weight: 650; font-size: 15px; margin-bottom: 6px; }
.quota-head { display: flex; justify-content: space-between; font-size: 13px; font-weight: 600; margin-bottom: 4px; }
.quota-val { text-align: right; font-size: 12.5px; font-weight: 600; margin-top: 2px; font-variant-numeric: tabular-nums; }
.small { font-size: 12.5px; }
.switch-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 24px; }
.switch-row { display: grid; grid-template-columns: 110px minmax(0, 1fr); gap: 8px; font-size: 12.5px; margin-bottom: 6px; }
.usage-row { display: grid; grid-template-columns: 90px minmax(0, 1fr) 90px; gap: 12px; align-items: center; font-size: 13px; margin-bottom: 8px; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
@media (max-width: 900px) {
  .tiles, .accounts, .switch-grid, .host-grid { grid-template-columns: 1fr; }
}
</style>
