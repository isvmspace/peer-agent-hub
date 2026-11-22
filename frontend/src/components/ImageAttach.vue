<script setup>
// 给消息带图片：粘贴截图、拖进来或点按钮选择。包住输入框使用，v-model 是 [{ name, data(dataURL), size }]。
// 大图在浏览器里先缩小（最长边 2560），每条最多 6 张、每张不超过 5MB，和 Hub 的限制一致。
import { ref } from 'vue'
import { useMessage } from 'naive-ui'
import { ImagePlus, X } from '@lucide/vue'
import { t } from '../i18n'

const MAX_IMAGES = 6
const MAX_BYTES = 5 * 1024 * 1024
const MAX_SIDE = 2560
const TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp']

const images = defineModel({ type: Array, default: () => [] })
const props = defineProps({ disabled: { type: Boolean, default: false } })
const message = useMessage()
const picker = ref(null)
const dragging = ref(false)

const readAsDataURL = blob => new Promise((resolve, reject) => {
  const r = new FileReader()
  r.onload = () => resolve(r.result)
  r.onerror = () => reject(r.error)
  r.readAsDataURL(blob)
})

// screenshots are often large PNGs: scale down and re-encode when too big; GIFs stay as they are (animation)
async function shrink(file) {
  if (file.type === 'image/gif') return file
  const bitmap = await createImageBitmap(file).catch(() => null)
  if (!bitmap) return file
  const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height))
  if (scale === 1 && file.size <= 2 * 1024 * 1024) return file
  const canvas = document.createElement('canvas')
  canvas.width = Math.round(bitmap.width * scale)
  canvas.height = Math.round(bitmap.height * scale)
  canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height)
  const type = file.type === 'image/png' ? 'image/png' : 'image/jpeg'
  let blob = await new Promise(resolve => canvas.toBlob(resolve, type, 0.9))
  if (blob && blob.size > MAX_BYTES && type === 'image/png') blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.85))
  return blob && blob.size < file.size ? blob : file
}

async function add(files) {
  if (props.disabled) return
  for (const file of files) {
    if (!TYPES.includes(file.type)) continue
    if (images.value.length >= MAX_IMAGES) {
      message.warning(t('每条消息最多带 {n} 张图片', { n: MAX_IMAGES }))
      break
    }
    const blob = await shrink(file)
    if (blob.size > MAX_BYTES) {
      message.warning(t('{name} 太大（超过 5MB）', { name: file.name || t('图片') }))
      continue
    }
    const ext = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp' }[blob.type || file.type] || 'png'
    const name = (file.name && file.name !== 'image.png' ? file.name.replace(/\.[^.]+$/, '') : `截图-${images.value.length + 1}`) + '.' + ext
    images.value = [...images.value, { name, data: await readAsDataURL(blob), size: blob.size }]
  }
}

function onPaste(e) {
  const files = [...(e.clipboardData?.files || [])].filter(f => f.type.startsWith('image/'))
  if (!files.length) return // plain text pastes as usual
  e.preventDefault()
  add(files)
}
function onDrop(e) {
  dragging.value = false
  const files = [...(e.dataTransfer?.files || [])].filter(f => f.type.startsWith('image/'))
  if (files.length) add(files)
}
function onPick(e) {
  add([...e.target.files])
  e.target.value = ''
}
function remove(i) {
  images.value = images.value.filter((_, j) => j !== i)
}

// what the API takes: [{ name, data }]
defineExpose({ pick: () => picker.value?.click() })
</script>

<template>
  <div :class="['image-attach', { dragging }]" @paste="onPaste" @dragover.prevent="dragging = !disabled" @dragleave="dragging = false" @drop.prevent="onDrop">
    <slot />
    <div v-if="images.length" class="image-attach__list">
      <div v-for="(img, i) in images" :key="i" class="image-attach__item" :title="img.name">
        <img :src="img.data" :alt="img.name">
        <button type="button" class="image-attach__remove" :aria-label="t('移除 {name}', { name: img.name })" @click="remove(i)"><X :size="12" /></button>
      </div>
    </div>
    <div class="image-attach__bar">
      <n-button size="tiny" quaternary :disabled="disabled || images.length >= MAX_IMAGES" @click="picker?.click()">
        <template #icon><ImagePlus :size="14" /></template>{{ t('图片') }}
      </n-button>
      <span class="image-attach__hint">{{ t('可粘贴截图或拖入图片，最多 {n} 张', { n: MAX_IMAGES }) }}</span>
      <input ref="picker" type="file" accept="image/png,image/jpeg,image/gif,image/webp" multiple hidden @change="onPick">
    </div>
  </div>
</template>

<style scoped>
.image-attach { display: flex; flex-direction: column; gap: 6px; min-width: 0; flex: 1; border-radius: 8px; }
.image-attach.dragging { outline: 2px dashed var(--primary-color, #18a058); outline-offset: 2px; }
.image-attach__list { display: flex; gap: 8px; flex-wrap: wrap; }
.image-attach__item { position: relative; width: 64px; height: 64px; border-radius: 6px; overflow: hidden; border: 1px solid rgba(128,128,128,.3); }
.image-attach__item img { width: 100%; height: 100%; object-fit: cover; display: block; }
.image-attach__remove { position: absolute; top: 2px; right: 2px; width: 20px; height: 20px; border-radius: 50%; border: 0; padding: 0;
  display: grid; place-items: center; background: rgba(0,0,0,.6); color: #fff; cursor: pointer; }
.image-attach__bar { display: flex; align-items: center; gap: 8px; }
.image-attach__hint { font-size: 12px; opacity: .55; }
</style>
