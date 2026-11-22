<script setup>
import { onBeforeUnmount, ref, watch } from 'vue'
import { Paperclip } from '@lucide/vue'
import { getToken } from '../api/client'

// Files an agent produced (generated images etc.). They need the bearer token, so
// they are fetched as blobs instead of being linked directly.
const props = defineProps({ taskId: { type: Number, required: true }, files: { type: Array, default: () => [] } })
const urls = ref({})

async function load() {
  for (const f of props.files) {
    if (urls.value[f.id]) continue
    try {
      const r = await fetch(`/api/tasks/${props.taskId}/files/${f.id}`, { headers: { Authorization: `Bearer ${getToken()}` } })
      if (r.ok) urls.value = { ...urls.value, [f.id]: URL.createObjectURL(await r.blob()) }
    } catch { /* shown as a plain link */ }
  }
}

watch(() => props.files.map(f => f.id).join(), load, { immediate: true })
onBeforeUnmount(() => Object.values(urls.value).forEach(u => URL.revokeObjectURL(u)))
</script>

<template>
  <div v-if="files.length" class="task-files">
    <template v-for="f in files" :key="f.id">
      <n-image v-if="f.kind === 'image' && urls[f.id]" :src="urls[f.id]" :alt="f.name" width="220" object-fit="contain" class="task-files__img" />
      <a v-else :href="urls[f.id]" :download="f.name" class="task-files__file"><Paperclip :size="12" />{{ f.name }}</a>
    </template>
  </div>
</template>
