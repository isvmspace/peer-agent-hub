<script setup>
// Agent results are Markdown. Raw HTML is never rendered, links keep markdown-it's
// protocol check and open in a new tab, and images become plain links so a result
// cannot make the browser fetch remote content.
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
import { t } from '../i18n'

const props = defineProps({ text: { type: String, default: '' } })

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })
md.renderer.rules.image = (tokens, idx) => {
  const tok = tokens[idx]
  const src = md.utils.escapeHtml(tok.attrGet('src') || '')
  const alt = md.utils.escapeHtml(tok.content || src)
  return `<a href="${src}" target="_blank" rel="noopener noreferrer">[${t('图片')}] ${alt}</a>`
}
const defaultLink = md.renderer.rules.link_open || ((tokens, idx, options, env, self) => self.renderToken(tokens, idx, options))
md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  tokens[idx].attrSet('target', '_blank')
  tokens[idx].attrSet('rel', 'noopener noreferrer')
  return defaultLink(tokens, idx, options, env, self)
}

const html = computed(() => md.render(props.text || ''))
</script>

<template>
  <!-- eslint-disable-next-line vue/no-v-html -- html:false above, so only markdown-generated markup -->
  <div class="md" v-html="html" />
</template>
