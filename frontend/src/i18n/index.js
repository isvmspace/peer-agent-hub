// Console language: Chinese source strings are the keys, i18n/en/*.js hold their English. Switching reloads the page,
// so nothing needs to be reactive and lists built once at import (menus, status labels) come out in the right language.
const KEY = 'agent-hub-lang'
export const LOCALES = [{ key: 'zh', label: '中文' }, { key: 'en', label: 'English' }]

function stored() {
  try {
    const value = localStorage.getItem(KEY)
    if (LOCALES.some(l => l.key === value)) return value
  } catch { /* storage blocked */ }
  return /^zh\b/i.test(navigator.language || '') ? 'zh' : 'en'
}

export const locale = stored()
document.documentElement.lang = locale === 'zh' ? 'zh-CN' : 'en'

const EN = Object.assign({}, ...Object.values(import.meta.glob('./en/*.js', { eager: true, import: 'default' })))

// t('共 {n} 个', { n: 3 }) — {name} placeholders are filled from params in either language
export function t(zh, params) {
  const text = locale === 'en' ? (EN[zh] ?? zh) : zh
  return params ? text.replace(/\{(\w+)\}/g, (m, k) => (k in params ? params[k] : m)) : text
}

export function setLocale(value) {
  if (value === locale) return
  try { localStorage.setItem(KEY, value) } catch { /* private mode */ }
  location.reload()
}
