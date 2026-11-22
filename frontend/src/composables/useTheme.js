import { computed, ref, watchEffect } from 'vue'

// light / dark / follow the system. index.html applies the same rule before first paint.
const KEY = 'agent-hub-theme'
const CHOICES = ['light', 'dark', 'system']
const media = window.matchMedia('(prefers-color-scheme: dark)')

function stored() {
  try {
    const value = localStorage.getItem(KEY)
    return CHOICES.includes(value) ? value : 'system'
  } catch { return 'system' }
}

const choice = ref(stored())
const systemDark = ref(media.matches)
media.addEventListener('change', e => { systemDark.value = e.matches })
// keep other open tabs in step
window.addEventListener('storage', e => { if (e.key === KEY) choice.value = stored() })

const mode = computed(() => (choice.value === 'system' ? (systemDark.value ? 'dark' : 'light') : choice.value))
watchEffect(() => { document.documentElement.dataset.theme = mode.value })

export function useTheme() {
  function setChoice(value) {
    choice.value = CHOICES.includes(value) ? value : 'system'
    try { localStorage.setItem(KEY, choice.value) } catch { /* private mode */ }
  }
  return { mode, choice, isDark: computed(() => mode.value === 'dark'), setChoice }
}
