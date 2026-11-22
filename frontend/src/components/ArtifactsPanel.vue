<script setup>
// Deliverables a task handed in (reports, diffs, test results, files), each with a
// version, a content hash and the git baseline it was produced against. The
// requester accepts one or sends it back; rework continues the same conversation.
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import { CircleCheck, Download, Eye, FileDiff, FileText, FlaskConical, Image, RotateCcw } from '@lucide/vue'
import { api, getToken } from '../api/client'
import { t } from '../i18n'
import { formatTime } from '../utils/format'

const props = defineProps({ task: { type: Object, required: true }, me: { type: Number, default: 0 } })
const emit = defineEmits(['changed'])
const message = useMessage()
const items = ref([])
const loaded = ref(false)
const loadError = ref('')
const preview = ref(null)
const rework = ref(null)
const note = ref('')
const busy = ref(false)
let seq = 0

const ICONS = { report: FileText, diff: FileDiff, test: FlaskConical, image: Image, file: FileText }
const KIND = { report: t('报告'), diff: t('代码改动'), test: t('测试结果'), image: t('图片'), file: t('文件') }
const REVIEW = { pending: { label: t('待验收'), type: 'default' }, accepted: { label: t('已通过'), type: 'success' }, rework: { label: t('需返工'), type: 'warning' } }
const canReview = computed(() => (props.task.from_user_id === props.me || props.task.turn_user_id === props.me)
  && !['canceled', 'rejected'].includes(props.task.status))
const running = computed(() => !['completed', 'failed', 'canceled', 'rejected'].includes(props.task.status))

async function load() {
  const mine = ++seq
  try {
    const result = await api(`/api/tasks/${props.task.id}/artifacts`)
    if (mine !== seq) return
    items.value = result.artifacts
    loaded.value = true
    loadError.value = ''
  } catch (e) {
    // keep what was shown before, and say it may be out of date
    if (mine === seq) loadError.value = e.message
  }
}
// another task's deliverables must never stay on screen, not even when this task's list fails to load
watch(() => props.task.id, () => { items.value = []; loaded.value = false; loadError.value = ''; rework.value = null; note.value = ''; closePreview() })
watch(() => [props.task.id, props.task.status, props.task.turn], load, { immediate: true })

// The request and reading its body are one network step: a drop in either says the same thing.
async function fetchBlob(a) {
  let response, body
  try {
    response = await fetch(`/api/artifacts/${a.id}`, { headers: { Authorization: `Bearer ${getToken()}` } })
    body = response.ok ? await response.blob() : await response.text()
  } catch (e) {
    if (e.name === 'AbortError') throw e
    throw new Error(t('网络连接失败，请检查网络后重试'))
  }
  if (!response.ok) {
    let text = body
    try { text = JSON.parse(body).error || body } catch { /* plain text */ }
    throw new Error(text || t('读取失败（{status}）', { status: response.status }))
  }
  return body
}

async function view(a) {
  try {
    const blob = await fetchBlob(a)
    const text = a.kind === 'image' ? null : await blob.text()
    if (a.task_id !== props.task.id) return  // the user moved to another task meanwhile
    closePreview()
    preview.value = a.kind === 'image' ? { a, url: URL.createObjectURL(blob) } : { a, text }
  } catch (e) { if (e.name !== 'AbortError') message.error(e.message) }
}

async function download(a) {
  try {
    const url = URL.createObjectURL(await fetchBlob(a))
    const link = Object.assign(document.createElement('a'), { href: url, download: a.name })
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 2000)
  } catch (e) { if (e.name !== 'AbortError') message.error(e.message) }
}

async function review(a, verdict) {
  busy.value = true
  try {
    const { artifact } = await api(`/api/artifacts/${a.id}/review`, { method: 'POST', body: { verdict, note: verdict === 'rework' ? note.value : '' } })
    message.success(verdict === 'accepted' ? t('{ref} 已验收通过', { ref: a.ref })
      : artifact.rework === 'queued' ? t('{ref} 已记录返工，本轮结束后自动发给智能体继续修改', { ref: a.ref })
        : t('{ref} 已要求返工，已在同一会话里发出新一轮', { ref: a.ref }))
    rework.value = null
    note.value = ''
    load()
    emit('changed')
  } catch (e) { message.error(e.message) } finally { busy.value = false }
}

const size = n => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`)
function closePreview() {
  if (preview.value?.url) URL.revokeObjectURL(preview.value.url)
  preview.value = null
}
onBeforeUnmount(closePreview)
</script>

<template>
  <div class="artifacts">
    <n-alert v-if="loadError" type="warning" :bordered="false" class="artifacts__error">
      {{ items.length ? t('刷新产物失败，下面是上次的结果：{error}', { error: loadError }) : t('加载产物失败：{error}', { error: loadError }) }}
      <n-button text type="primary" size="small" @click="load">{{ t('重试') }}</n-button>
    </n-alert>
    <n-skeleton v-if="!loaded && !loadError" text :repeat="2" />
    <p v-else-if="loaded && !items.length" class="hint">{{ t('还没有产物。智能体可用 agent-hub 的 submit_artifact 提交报告、diff 和测试结果，生成的图片和文件也会出现在这里。') }}</p>
    <article v-for="a in items" :key="a.id" class="artifact">
      <component :is="ICONS[a.kind] || FileText" :size="18" class="artifact__icon" />
      <div class="artifact__main">
        <strong><span class="artifact__ref">{{ a.ref }}</span>{{ a.name }} <small>v{{ a.version }}</small></strong>
        <small>{{ KIND[a.kind] || a.kind }} · {{ size(a.size) }} · sha256 <code :title="a.sha256">{{ a.sha256.slice(0, 12) }}</code></small>
        <small v-if="a.code_ref?.commit">{{ t('基于') }} <code :title="a.code_ref.commit">{{ a.code_ref.commit.slice(0, 8) }}</code>{{ a.code_ref.branch ? ' · ' + a.code_ref.branch : '' }}{{ a.code_ref.dirty === 'true' ? ' · ' + t('有未提交改动') : '' }}</small>
        <small v-if="a.review_note" class="artifact__note">{{ t('返工说明：') }}{{ a.review_note }}{{ a.rework_pending ? t('（本轮结束后自动发出）') : '' }}</small>
      </div>
      <n-tag size="small" :bordered="false" :type="REVIEW[a.review]?.type">{{ REVIEW[a.review]?.label }}</n-tag>
      <div class="artifact__actions">
        <n-button size="tiny" quaternary :aria-label="t('查看 {ref}', { ref: a.ref })" @click="view(a)"><template #icon><Eye /></template></n-button>
        <n-button size="tiny" quaternary :aria-label="t('下载 {ref}', { ref: a.ref })" @click="download(a)"><template #icon><Download /></template></n-button>
        <template v-if="canReview && a.review !== 'accepted'">
          <n-button size="tiny" secondary type="success" :loading="busy" @click="review(a, 'accepted')"><template #icon><CircleCheck /></template>{{ t('通过') }}</n-button>
          <n-button size="tiny" secondary type="warning" @click="rework = a"><template #icon><RotateCcw /></template>{{ t('返工') }}</n-button>
        </template>
      </div>
    </article>

    <n-modal :show="!!preview" preset="card" :title="preview ? `${preview.a.ref} ${preview.a.name} v${preview.a.version}` : ''" class="form-modal wide" @update:show="v => { if (!v) closePreview() }">
      <img v-if="preview?.url" :src="preview.url" :alt="preview.a.name" class="artifact-preview-img">
      <pre v-else class="artifact-preview">{{ preview?.text }}</pre>
      <template #footer><small class="muted">sha256 {{ preview?.a.sha256 }} · {{ t('提交于 {time}', { time: formatTime(preview?.a.created_at) }) }}</small></template>
    </n-modal>
    <n-modal :show="!!rework" preset="card" :title="t('要求返工 · {ref} {name}', { ref: rework?.ref, name: rework?.name })" class="form-modal" @update:show="v => { if (!v) rework = null }">
      <p class="hint">{{ running ? t('任务这一轮还在执行：返工说明会在本轮结束后自动发给智能体，在同一会话里继续修改。') : t('返工说明会作为下一轮消息发给智能体，在同一会话、同一工作区里继续修改。') }}</p>
      <n-input v-model:value="note" type="textarea" :autosize="{ minRows: 3, maxRows: 8 }" :placeholder="t('哪里不对、要改成什么样')" />
      <template #footer>
        <n-space justify="end">
          <n-button @click="rework = null">{{ t('取消') }}</n-button>
          <n-button type="warning" :disabled="!note.trim()" :loading="busy" @click="review(rework, 'rework')">{{ t('发送返工') }}</n-button>
        </n-space>
      </template>
    </n-modal>
  </div>
</template>
