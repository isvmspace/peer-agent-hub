<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { darkTheme, dateEnUS, dateZhCN, enUS, zhCN } from 'naive-ui'
import { locale } from './i18n'
import { useTheme } from './composables/useTheme'

const { isDark } = useTheme()

// styles/theme.css is the only palette; naive-ui gets the resolved values (it needs literal colours to derive shades)
const NAMES = ['signal', 'signal-hover', 'signal-pressed', 'on-signal', 'bg', 'surface', 'surface-2', 'surface-3', 'line', 'line-soft',
  'ink', 'ink-2', 'ink-3', 'ink-4', 'flow', 'ok', 'warn', 'err']
const tokens = ref(read())
function read() {
  const style = getComputedStyle(document.documentElement)
  return Object.fromEntries(NAMES.map(n => [n, style.getPropertyValue('--' + n).trim()]))
}
watch(isDark, () => nextTick(() => { tokens.value = read() }), { flush: 'post' })

// hover/pressed for the semantic colours: lighter on dark, darker on light
function shade(hex, amount) {
  const n = parseInt(hex.slice(1), 16)
  const target = amount > 0 ? 255 : 0
  const ch = i => Math.round(((n >> i) & 255) + (target - ((n >> i) & 255)) * Math.abs(amount))
  return '#' + [16, 8, 0].map(i => ch(i).toString(16).padStart(2, '0')).join('')
}
const semantic = (key, base, dark) => ({
  [`${key}Color`]: base,
  [`${key}ColorHover`]: shade(base, dark ? 0.18 : -0.12),
  [`${key}ColorPressed`]: shade(base, dark ? -0.12 : -0.22),
  [`${key}ColorSuppl`]: shade(base, dark ? 0.18 : -0.12),
})

const overrides = computed(() => {
  const t = tokens.value
  const dark = isDark.value
  const sem = Object.assign({}, ...[['info', 'flow'], ['success', 'ok'], ['warning', 'warn'], ['error', 'err']]
    .map(([key, token]) => semantic(key, t[token], dark)))
  return {
    common: {
      fontFamily: 'var(--font-sans)',
      fontFamilyMono: 'var(--font-mono)',
      primaryColor: t.signal, primaryColorHover: t['signal-hover'], primaryColorPressed: t['signal-pressed'], primaryColorSuppl: t['signal-hover'],
      ...sem,
      bodyColor: t.bg, cardColor: t.surface, modalColor: t.surface, popoverColor: t.surface, tableColor: t.surface,
      inputColor: dark ? t['surface-2'] : t.surface, actionColor: t['surface-2'],
      borderColor: t.line, dividerColor: t['line-soft'],
      textColorBase: t.ink, textColor1: t.ink, textColor2: t['ink-2'], textColor3: t['ink-3'], placeholderColor: t['ink-4'],
      borderRadius: '8px', borderRadiusSmall: '6px',
    },
    Button: {
      fontWeight: '600',
      textColorPrimary: t['on-signal'], textColorHoverPrimary: t['on-signal'], textColorPressedPrimary: t['on-signal'], textColorFocusPrimary: t['on-signal'],
    },
    Card: { borderRadius: '12px', color: t.surface, borderColor: t.line, paddingMedium: '18px 20px' },
    DataTable: { thColor: t['surface-2'], thColorHover: t['surface-2'], tdColor: t.surface, tdColorHover: t['surface-2'], borderColor: t['line-soft'], thTextColor: t['ink-2'], thFontWeight: '600' },
    Input: { color: dark ? t['surface-2'] : t.surface, colorFocus: dark ? t['surface-2'] : t.surface, border: `1px solid ${t.line}` },
    Tabs: { tabTextColorActiveLine: t.ink, barColor: t.signal, colorSegment: t['surface-2'], tabColorSegment: dark ? t['surface-3'] : t.surface },
    Layout: { color: t.bg, siderColor: t.surface, headerColor: t.surface },
    Menu: {
      itemColorActive: dark ? 'rgba(60,207,230,.12)' : '#e1f0f5',
      itemColorActiveHover: dark ? 'rgba(60,207,230,.16)' : '#d6eaf1',
      itemColorHover: t['surface-2'],
      itemTextColorActive: t.signal, itemTextColorActiveHover: t.signal, itemIconColorActive: t.signal, itemIconColorActiveHover: t.signal,
      itemTextColor: t['ink-2'], itemIconColor: t['ink-3'], groupTextColor: t['ink-3'], itemBorderRadius: '8px', itemHeight: '38px',
    },
  }
})
</script>

<template>
  <n-config-provider :locale="locale === 'zh' ? zhCN : enUS" :date-locale="locale === 'zh' ? dateZhCN : dateEnUS" :theme="isDark ? darkTheme : null" :theme-overrides="overrides">
    <n-global-style />
    <n-message-provider>
      <n-dialog-provider>
        <router-view />
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>
