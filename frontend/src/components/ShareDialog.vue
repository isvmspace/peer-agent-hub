<script setup>
import { computed, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import { api } from '../api/client'
import { useAuth } from '../composables/useAuth'
import { t } from '../i18n'

// Share one conversation with friends: read-only, or allowed to keep asking in the same session.
const props = defineProps({ show: Boolean, task: { type: Object, default: null } })
const emit = defineEmits(['update:show'])
const message = useMessage()
const { state: auth } = useAuth()
const friends = ref([])
const shares = ref([])
const form = ref({ user: null, can_continue: true, auto_approve: false })
const busy = ref(false)
const isOwner = computed(() => props.task?.owner_user_id === auth.user?.id)
const options = computed(() => friends.value
  .filter(f => ![props.task?.from_user_id, props.task?.owner_user_id].includes(f.user_id))
  .map(f => ({ label: `${f.display_name || f.username}（${f.username}）`, value: f.username })))

async function load() {
  try {
    friends.value = (await api('/api/friends')).friends
    shares.value = (await api(`/api/tasks/${props.task.id}/shares`)).shares
  } catch (e) { message.error(e.message) }
}

async function share() {
  busy.value = true
  try {
    shares.value = (await api(`/api/tasks/${props.task.id}/shares`, { method: 'POST', body: form.value })).shares
    message.success(t('已分享'))
    form.value.user = null
  } catch (e) { message.error(e.message) } finally { busy.value = false }
}

async function revoke(s) {
  try { shares.value = (await api(`/api/tasks/${props.task.id}/shares/${s.user_id}`, { method: 'DELETE' })).shares } catch (e) { message.error(e.message) }
}

watch(() => props.show, v => { if (v) load() })
</script>

<template>
  <n-modal :show="show" preset="card" :title="t('分享对话 · #{id}', { id: task?.id })" class="form-modal" @update:show="v => emit('update:show', v)">
    <p class="hint">{{ t('只能分享给好友。对方能看到整段对话和生成的文件；允许继续对话时，对方的追问会在同一会话、同一工作区里由 {agent} 执行。', { agent: task?.agent_address }) }}</p>
    <n-form label-placement="left" label-width="96">
      <n-form-item :label="t('分享给')"><n-select v-model:value="form.user" :options="options" filterable :placeholder="t('选择好友')" /></n-form-item>
      <n-form-item :label="t('继续对话')"><n-switch v-model:value="form.can_continue" /><span class="hint inline">{{ t('关闭则只读') }}</span></n-form-item>
      <n-form-item v-if="isOwner && form.can_continue" :label="t('追问免审批')"><n-switch v-model:value="form.auto_approve" /><span class="hint inline">{{ t('关闭时对方每次追问都要你批准') }}</span></n-form-item>
    </n-form>
    <n-space justify="end"><n-button type="primary" :loading="busy" :disabled="!form.user" @click="share">{{ t('分享') }}</n-button></n-space>
    <n-divider title-placement="left">{{ t('已分享') }}</n-divider>
    <div v-if="!shares.length" class="hint">{{ t('还没有分享给任何人') }}</div>
    <div v-for="s in shares" :key="s.user_id" class="share-row">
      <span>{{ s.display_name || s.username }}</span>
      <n-tag size="small" :type="s.can_continue ? 'success' : 'default'">{{ s.can_continue ? (s.auto_approve ? t('可继续 · 免审批') : t('可继续 · 需审批')) : t('只读') }}</n-tag>
      <n-button size="tiny" quaternary type="error" @click="revoke(s)">{{ t('取消分享') }}</n-button>
    </div>
  </n-modal>
</template>
