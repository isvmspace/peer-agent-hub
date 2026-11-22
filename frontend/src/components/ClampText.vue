<script setup>
// 长文本先显示约 12 行，按需「展开全文」；可选按 Markdown 渲染。让外层只有一个滚动条，不再滚动套滚动。
import { nextTick, onMounted, ref, watch } from 'vue'
import MarkdownText from './MarkdownText.vue'
import { t } from '../i18n'

const props = defineProps({ text: { type: String, default: '' }, markdown: { type: Boolean, default: false } })
const box = ref(null)
const long = ref(false)
const open = ref(false)

async function measure() {
  await nextTick()
  long.value = !!box.value && box.value.scrollHeight > box.value.clientHeight + 4
}
onMounted(measure)
watch(() => props.text, () => { open.value = false; measure() })
</script>

<template>
  <div class="clamp-text">
    <div ref="box" :class="['clamp-body', { open, fade: long && !open }]">
      <MarkdownText v-if="markdown" :text="text" />
      <template v-else>{{ text }}</template>
    </div>
    <button v-if="long || open" type="button" class="clamp-toggle" :aria-expanded="open" @click="open = !open; measure()">
      {{ open ? t('收起') : t('展开全文') }}
    </button>
  </div>
</template>

<style scoped>
.clamp-body { max-height: 20.4em; overflow: hidden; white-space: pre-wrap; word-break: break-word; position: relative; }
.clamp-body :deep(p), .clamp-body :deep(ul), .clamp-body :deep(ol) { white-space: normal; }
.clamp-body.open { max-height: none; }
.clamp-body.fade::after { content: ''; position: absolute; left: 0; right: 0; bottom: 0; height: 2.4em; pointer-events: none;
  background: linear-gradient(transparent, var(--clamp-fade, var(--surface))); }
.clamp-toggle { margin-top: 6px; padding: 2px 0; border: 0; background: none; color: var(--signal-strong); font: inherit; font-size: 12px; cursor: pointer; }
</style>
