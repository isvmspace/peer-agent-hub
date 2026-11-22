<script setup>
import { computed, h, onMounted, ref } from 'vue'
import { NButton, NTag, useDialog, useMessage } from 'naive-ui'
import { Copy, KeyRound, PlugZap } from '@lucide/vue'
import PageHeader from '../components/PageHeader.vue'
import { api } from '../api/client'
import { t } from '../i18n'
import { formatTime } from '../utils/format'

import ResponsiveTable from '../components/ResponsiveTable.vue'
const message = useMessage()
const dialog = useDialog()
const tokens = ref([])
const connectors = ref([])
const downloads = ref([])
const created = ref(null)
const tokenName = ref('')
const origin = window.location.origin
const mcpUrl = `${origin}/mcp/`

const personalToken = computed(() => (created.value?.kind === 'personal' ? created.value.token : t('ahp_你的个人令牌')))
const connectorToken = computed(() => (created.value?.kind === 'connector' ? created.value.token : t('ahc_你的连接器令牌')))

const snippets = computed(() => ({
  winConnector: [
    `# ${t('PowerShell：下载连接器到用户目录')}`,
    `mkdir $HOME\\agent-hub -Force; cd $HOME\\agent-hub`,
    `Invoke-WebRequest ${origin}/downloads/latest/windows-amd64 -OutFile agent-hub-connector.exe`,
    `.\\agent-hub-connector.exe init --hub ${origin} --token ${connectorToken.value}`,
    `.\\agent-hub-connector.exe run        # ${t('前台试运行，确认已连接后 Ctrl+C')}`,
    `.\\agent-hub-connector.exe install    # ${t('登录自启（计划任务，无窗口）')}`,
  ].join('\n'),
  macConnector: [
    `mkdir -p ~/.local/bin && cd ~/.local/bin`,
    `curl -fLo agent-hub-connector ${origin}/downloads/latest/darwin-arm64   # ${t('Intel 芯片用 darwin-amd64')}`,
    `chmod +x agent-hub-connector && xattr -d com.apple.quarantine agent-hub-connector 2>/dev/null`,
    `./agent-hub-connector init --hub ${origin} --token ${connectorToken.value}`,
    `./agent-hub-connector run       # ${t('前台试运行')}`,
    `./agent-hub-connector install   # ${t('登录自启（launchd）')}`,
  ].join('\n'),
  claude: `claude mcp add --transport http --scope user agent-hub ${mcpUrl} --header "Authorization: Bearer ${personalToken.value}"`,
  codex: [
    `# ${t('macOS / Linux：把令牌写进 shell 配置（~/.zshrc 等）')}`,
    `export AGENT_HUB_TOKEN=${personalToken.value}`,
    `# Windows: setx AGENT_HUB_TOKEN ${personalToken.value}  ${t('（执行后重开终端）')}`,
    `codex mcp add agent-hub --url ${mcpUrl} --bearer-token-env-var AGENT_HUB_TOKEN`,
  ].join('\n'),
  hermes: [
    `hermes mcp add agent-hub --url ${mcpUrl} --auth header`,
    `# ${t('按提示填入请求头')}  Authorization: Bearer ${personalToken.value}`,
  ].join('\n'),
}))

async function load() {
  try {
    const [t, c, d] = await Promise.all([api('/api/tokens'), api('/api/connectors'), api('/api/downloads')])
    tokens.value = t.tokens
    connectors.value = c.connectors
    downloads.value = d.files
  } catch (e) {
    message.error(e.message)
  }
}

async function createToken(kind) {
  try {
    const name = tokenName.value.trim() || (kind === 'connector' ? t('我的电脑') : t('我的 MCP'))
    const result = await api('/api/tokens', { method: 'POST', body: { kind, name } })
    created.value = { kind, token: result.token }
    tokenName.value = ''
    load()
  } catch (e) {
    message.error(e.message)
  }
}

function revoke(row) {
  dialog.warning({
    title: t('吊销令牌'),
    content: row.kind === 'connector' ? t('吊销「{name}」后，使用它的连接器会立即下线，且不可恢复。', { name: row.name }) : t('吊销「{name}」后，使用它的智能体将无法调用 MCP，且不可恢复。', { name: row.name }),
    positiveText: t('吊销'),
    negativeText: t('取消'),
    onPositiveClick: async () => {
      try { await api(`/api/tokens/${row.id}`, { method: 'DELETE' }); message.success(t('已吊销')); load() } catch (e) { message.error(e.message) }
    },
  })
}

async function copy(text) {
  try { await navigator.clipboard.writeText(text); message.success(t('已复制')) } catch { message.warning(t('复制失败，请手动选择')) }
}

const tokenColumns = [
  { title: t('名称'), key: 'name' },
  { title: t('类型'), key: 'kind', width: 110, render: r => h(NTag, { size: 'small', bordered: false, type: r.kind === 'connector' ? 'info' : 'success' }, () => (r.kind === 'connector' ? t('连接器') : t('个人 MCP'))) },
  { title: t('前缀'), key: 'prefix', width: 120, render: r => `${r.prefix}…` },
  { title: t('创建'), key: 'created_at', width: 140, render: r => formatTime(r.created_at) },
  { title: t('最近使用'), key: 'last_used_at', width: 140, render: r => formatTime(r.last_used_at) },
  { title: t('状态'), key: 'revoked', width: 80, render: r => (r.revoked ? h(NTag, { size: 'small' }, () => t('已吊销')) : h(NTag, { size: 'small', type: 'success', bordered: false }, () => t('有效'))) },
  { title: '', key: 'a', width: 80, render: r => (r.revoked ? null : h(NButton, { size: 'small', quaternary: true, type: 'error', onClick: () => revoke(r) }, () => t('吊销'))) },
]
const connectorColumns = [
  { title: t('连接器'), key: 'name', render: r => h('div', { class: 'primary-cell' }, [h('strong', null, [h('span', { class: ['dot', r.online ? 'on' : 'off'] }), r.name]), h('small', null, `${r.hostname} · ${r.os} · v${r.version}`)]) },
  { title: t('智能体'), key: 'agents', render: r => r.agents.map(a => `${a.key}${a.online ? '' : t('(离线)')}`).join(t('、')) || '—' },
  { title: t('令牌'), key: 'prefix', width: 120, render: r => `${r.prefix}…` },
  { title: t('最近心跳'), key: 'last_seen_at', width: 140, render: r => formatTime(r.last_seen_at) },
]

onMounted(load)
</script>

<template>
  <div class="page-wrap">
    <PageHeader :title="t('接入配置')" :description="t('① 连接器：把本机智能体注册上来接收任务；② 个人令牌：让你的智能体通过 MCP 给别人派任务')">
      <template #actions>
        <n-button tag="a" :href="`${origin}/docs/onboarding.md`" target="_blank">{{ t('对接手册') }}</n-button>
      </template>
    </PageHeader>
    <n-alert type="info" :bordered="false" class="chat-alert">
      {{ t('最省事的接入方式：创建下面两个令牌后，对你的 Claude Code / Codex 说：') }}「{{ t('请按 {origin}/docs/onboarding.md 帮我接入 Agent Hub，连接器令牌 ahc_…，个人令牌 ahp_…', { origin }) }}」{{ t('，它会自己完成配置。') }}
    </n-alert>

    <n-card :title="t('创建令牌')">
      <n-space align="center">
        <n-input v-model:value="tokenName" :placeholder="t('令牌名称，如 公司 Windows 电脑')" style="width: 280px" />
        <n-button type="primary" @click="createToken('connector')"><template #icon><PlugZap /></template>{{ t('创建连接器令牌') }}</n-button>
        <n-button @click="createToken('personal')"><template #icon><KeyRound /></template>{{ t('创建个人 MCP 令牌') }}</n-button>
      </n-space>
      <n-alert v-if="created" type="success" class="token-alert" :title="created.kind === 'connector' ? t('连接器令牌（只显示这一次，请立即保存）') : t('个人 MCP 令牌（只显示这一次，请立即保存）')">
        <div class="token-line"><code>{{ created.token }}</code><n-button size="small" @click="copy(created.token)"><template #icon><Copy /></template>{{ t('复制') }}</n-button></div>
        <div>{{ t('下方的命令已自动填入该令牌。') }}</div>
      </n-alert>
    </n-card>

    <n-card :title="t('① 安装连接器（接收任务）')">
      <p class="hint">{{ t('连接器会自动发现本机 PATH 中的 claude / codex / hermes，收到的任务默认隔离执行（只能改授权工作区里的文件），别人派来的任务还要你审批。配置文件可手动编辑工作目录、标签和自定义命令型智能体。') }}</p>
      <n-tabs type="segment" animated>
        <n-tab-pane name="win" tab="Windows">
          <div class="code-block"><pre>{{ snippets.winConnector }}</pre><n-button size="tiny" @click="copy(snippets.winConnector)">{{ t('复制') }}</n-button></div>
          <p class="hint">{{ t('配置文件：{path}，日志：同目录 connector.log', { path: '%AppData%\\agent-hub\\connector.json' }) }}</p>
        </n-tab-pane>
        <n-tab-pane name="mac" tab="macOS">
          <div class="code-block"><pre>{{ snippets.macConnector }}</pre><n-button size="tiny" @click="copy(snippets.macConnector)">{{ t('复制') }}</n-button></div>
          <p class="hint">{{ t('配置文件：{path}', { path: '~/Library/Application Support/agent-hub/connector.json' }) }}</p>
        </n-tab-pane>
      </n-tabs>
      <p v-if="downloads.length" class="hint">{{ t('最新版本下载：') }}<a v-for="f in downloads" :key="f.platform" :href="f.url" class="dl-link">{{ f.platform }} v{{ f.version }}</a>{{ t('（连接器会自动升级）') }}</p>
      <n-alert v-else type="warning" class="token-alert" :title="t('还没有发布连接器')">{{ t('请管理员在「连接器版本」页上传，或用镜像内置版本启动。') }}</n-alert>
    </n-card>

    <n-card :title="t('② 给智能体接入 MCP（不打开 Hub 也能聊天、派任务）')">
      <p class="hint">{{ t('接入后直接在 Claude Code / Codex 里说就行，例如：') }}</p>
      <ul class="hint mcp-examples">
        <li>{{ t('“agent-hub 有没有新消息？” —— 未读私聊、群聊、好友申请、等你审批的任务') }}</li>
        <li>{{ t('“告诉 zhangsan 客户端包好了，等他回复” —— 私聊好友并等待回复') }}</li>
        <li>{{ t('“用 zhangsan 的 claude 在他电脑上打包桌面客户端并安装，等结果告诉我” —— 派任务、继续追问') }}</li>
        <li>{{ t('“在『发版群』里 @codex 跑一下单测” —— 群聊发言，@ 的智能体会干活') }}</li>
      </ul>
      <p class="hint">{{ t('MCP 地址：') }}<code>{{ mcpUrl }}</code>{{ t('，令牌只需配置一次。') }}</p>
      <n-tabs type="segment" animated>
        <n-tab-pane name="claude" tab="Claude Code">
          <div class="code-block"><pre>{{ snippets.claude }}</pre><n-button size="tiny" @click="copy(snippets.claude)">{{ t('复制') }}</n-button></div>
        </n-tab-pane>
        <n-tab-pane name="codex" tab="Codex">
          <div class="code-block"><pre>{{ snippets.codex }}</pre><n-button size="tiny" @click="copy(snippets.codex)">{{ t('复制') }}</n-button></div>
        </n-tab-pane>
        <n-tab-pane name="hermes" tab="Hermes">
          <div class="code-block"><pre>{{ snippets.hermes }}</pre><n-button size="tiny" @click="copy(snippets.hermes)">{{ t('复制') }}</n-button></div>
        </n-tab-pane>
      </n-tabs>
    </n-card>

    <n-card :title="t('我的连接器')">
      <ResponsiveTable :columns="connectorColumns" :data="connectors" :row-key="r => r.id" />
    </n-card>
    <n-card :title="t('我的令牌')">
      <ResponsiveTable :columns="tokenColumns" :data="tokens" :row-key="r => r.id" />
    </n-card>
  </div>
</template>
