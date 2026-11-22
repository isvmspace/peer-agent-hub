<script setup>
import { computed, h, onMounted, reactive, ref } from 'vue'
import { NButton, NTag, useDialog, useMessage } from 'naive-ui'
import { Download, Upload } from '@lucide/vue'
import { RouterLink } from 'vue-router'
import PageHeader from '../components/PageHeader.vue'
import { api, getToken } from '../api/client'
import { useAuth } from '../composables/useAuth'
import { t } from '../i18n'
import { formatTime } from '../utils/format'

import ResponsiveTable from '../components/ResponsiveTable.vue'
const message = useMessage()
const dialog = useDialog()
const { state: auth } = useAuth()
const releases = ref([])
const downloads = ref([])
const origin = window.location.origin
const PLATFORM_INFO = {
  'windows/amd64': { name: 'Windows', sub: 'x64', file: 'agent-hub-connector.exe' },
  'darwin/arm64': { name: 'macOS', sub: t('Apple 芯片（M1/M2/M3/M4）'), file: 'agent-hub-connector' },
  'darwin/amd64': { name: 'macOS', sub: t('Intel 芯片'), file: 'agent-hub-connector' },
  'linux/amd64': { name: 'Linux', sub: 'x86_64', file: 'agent-hub-connector' },
}
const ua = navigator.userAgent
const myPlatform = /Windows/.test(ua) ? 'windows/amd64' : /Mac/.test(ua) ? 'darwin/arm64' : /Linux/.test(ua) ? 'linux/amd64' : ''
const latestVersion = computed(() => downloads.value.map(f => f.version).sort((a, b) => b.localeCompare(a, undefined, { numeric: true }))[0] || '')
const sortedDownloads = computed(() => [...downloads.value].sort((a, b) => (b.platform === myPlatform) - (a.platform === myPlatform)))
const platforms = ref([])
const storage = ref('')
const busy = ref(false)
const file = ref(null)
const form = reactive({ version: '', platform: 'windows/amd64', policy: 'OPTIONAL', min_supported: '', notes: '' })

async function load() {
  try {
    const [data, d] = await Promise.all([api('/api/releases'), api('/api/downloads')])
    downloads.value = d.files
    releases.value = data.releases
    platforms.value = data.platforms
    storage.value = data.storage
  } catch (e) { message.error(e.message) }
}

function pick(event) {
  file.value = event.target.files?.[0] || null
  const m = file.value?.name.match(/agent-hub-connector-(windows|darwin|linux)-(amd64|arm64)/)
  if (m) form.platform = `${m[1]}/${m[2]}`
}

async function upload() {
  if (!file.value || !form.version) {
    message.warning(t('请选择安装包并填写版本号'))
    return
  }
  busy.value = true
  try {
    const q = new URLSearchParams({ ...form })
    const res = await fetch(`/api/releases?${q}`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${getToken()}`, 'Content-Type': 'application/octet-stream' },
      body: file.value,
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.error || t('上传失败'))
    message.success(t('已发布 {platform} {version}，在线连接器将收到升级通知', { platform: data.release.platform, version: data.release.version }))
    file.value = null
    load()
  } catch (e) {
    message.error(e.message)
  } finally {
    busy.value = false
  }
}

function withdraw(row) {
  dialog.warning({
    title: t('撤回版本'),
    content: t('撤回 {platform} {version}？已升级的连接器不会回退，未升级的将不再收到该版本。', { platform: row.platform, version: row.version }),
    positiveText: t('撤回'),
    negativeText: t('取消'),
    onPositiveClick: async () => {
      try { await api(`/api/releases/${row.id}/withdraw`, { method: 'POST' }); load() } catch (e) { message.error(e.message) }
    },
  })
}

const columns = [
  { title: t('版本'), key: 'version', width: 90 },
  { title: t('平台'), key: 'platform', width: 140 },
  { title: t('策略'), key: 'policy', width: 110, render: r => h(NTag, { size: 'small', bordered: false, type: r.policy === 'FORCED' ? 'error' : 'info' }, () => (r.policy === 'FORCED' ? t('强制') : t('可选'))) },
  { title: t('最低支持'), key: 'min_supported', width: 90, render: r => r.min_supported || '—' },
  { title: t('大小'), key: 'size', width: 90, render: r => `${(r.size / 1048576).toFixed(1)} MB` },
  { title: 'SHA-256', key: 'sha256', render: r => h('code', { class: 'muted' }, r.sha256.slice(0, 16) + '…') },
  { title: t('说明'), key: 'notes' },
  { title: t('发布'), key: 'created_at', width: 150, render: r => `${formatTime(r.created_at)} ${r.publisher || t('镜像内置')}` },
  { title: t('状态'), key: 'status', width: 80, render: r => (r.status === 'published' ? h(NTag, { size: 'small', type: 'success', bordered: false }, () => t('已发布')) : h(NTag, { size: 'small' }, () => t('已撤回'))) },
  { title: '', key: 'a', width: 70, render: r => (auth.user?.is_admin && r.status === 'published' ? h(NButton, { size: 'small', quaternary: true, type: 'error', onClick: () => withdraw(r) }, () => t('撤回')) : null) },
]

onMounted(load)
</script>

<template>
  <div class="page-wrap">
    <PageHeader
      :title="auth.user?.is_admin ? t('连接器版本') : t('下载连接器')"
      :description="auth.user?.is_admin
        ? t('下载页对所有人可见；安装包存放在 {where}，发布新版本后在线连接器会自动升级。', { where: storage === 'minio' ? 'MinIO' : t('Hub 本地目录') })
        : t('连接器装在你自己的电脑上，把本机的 Claude Code / Codex / Hermes 接入 Agent Hub。装好后会自动升级，不需要再来这里。')"
    />

    <n-card :title="latestVersion ? t('最新版本 v{version}', { version: latestVersion }) : t('下载')">
      <n-empty v-if="!downloads.length" :description="t('管理员还没有发布连接器')" />
      <div v-else class="download-grid">
        <div v-for="f in sortedDownloads" :key="f.platform" :class="['download-card', { recommended: f.platform === myPlatform }]">
          <div class="download-card__head">
            <strong>{{ PLATFORM_INFO[f.platform]?.name || f.platform }}</strong>
            <n-tag v-if="f.platform === myPlatform" size="small" type="success" :bordered="false">{{ t('推荐') }}</n-tag>
          </div>
          <small>{{ PLATFORM_INFO[f.platform]?.sub }} · v{{ f.version }} · {{ (f.size / 1048576).toFixed(1) }} MB</small>
          <small class="muted" :title="f.sha256">SHA-256 {{ f.sha256.slice(0, 12) }}…</small>
          <p v-if="f.notes" class="download-card__notes">{{ f.notes }}</p>
          <n-button tag="a" :href="f.url" :download="PLATFORM_INFO[f.platform]?.file" type="primary" :secondary="f.platform !== myPlatform" block>
            <template #icon><Download /></template>{{ t('下载') }}
          </n-button>
        </div>
      </div>
    </n-card>

    <n-card :title="t('安装与升级')">
      <ol class="install-steps">
        <li>{{ t('在「') }}<RouterLink to="/connect">{{ t('接入配置') }}</RouterLink>{{ t('」创建一个') }}<b>{{ t('连接器令牌') }}</b>{{ t('，那里的命令已自动填好下载地址和令牌，复制到终端执行即可。') }}</li>
        <li>{{ t('先执行') }} <code>agent-hub-connector init --hub {{ origin }} --token ahc_…</code>{{ t('，再') }} <code>run</code> {{ t('试运行，确认连上后') }} <code>install</code> {{ t('设为登录自启。') }}</li>
        <li><b>{{ t('自动升级：') }}</b>{{ t('连接器上线时和每 6 小时检查一次新版本，空闲时自动下载、校验 SHA-256 并重启，不打断正在执行的任务。也可以手动执行') }} <code>agent-hub-connector update</code>{{ t('。') }}</li>
        <li>{{ t('想在自己的智能体里直接聊天、派任务？在「接入配置」创建') }}<b>{{ t('个人 MCP 令牌') }}</b>{{ t('并按提示添加 agent-hub MCP。') }}</li>
      </ol>
    </n-card>

    <template v-if="auth.user?.is_admin">
      <n-card :title="t('发布新版本')">
        <n-space align="center" :wrap="true">
          <input type="file" @change="pick" />
          <n-input v-model:value="form.version" :placeholder="t('版本号 x.y.z')" style="width: 130px" />
          <n-select v-model:value="form.platform" :options="platforms.map(p => ({ label: p, value: p }))" style="width: 170px" />
          <n-select v-model:value="form.policy" :options="[{ label: t('可选更新'), value: 'OPTIONAL' }, { label: t('强制更新'), value: 'FORCED' }]" style="width: 120px" />
          <n-input v-model:value="form.min_supported" :placeholder="t('最低支持版本（可选）')" style="width: 170px" />
          <n-input v-model:value="form.notes" :placeholder="t('更新说明')" style="width: 240px" />
          <n-button type="primary" :loading="busy" @click="upload"><template #icon><Upload /></template>{{ t('上传并发布') }}</n-button>
        </n-space>
        <p class="hint" style="margin-top: 12px">{{ t('也可以在服务器上执行') }} <code>scripts/publish-connector.sh</code> {{ t('批量发布四个平台。低于「最低支持版本」的连接器会被强制升级。') }}</p>
      </n-card>
      <n-card :title="t('版本列表')">
        <ResponsiveTable :columns="columns" :data="releases" :row-key="r => r.id" />
      </n-card>
    </template>
  </div>
</template>
