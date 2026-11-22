import { locale } from '../i18n'

const TOKEN_KEY = 'agent-hub-token'
let unauthorizedHandler = null

export function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || '' } catch { return '' }
}

export function setToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch { /* storage unavailable */ }
}

export function setUnauthorizedHandler(handler) {
  unauthorizedHandler = handler
}

export async function api(url, options = {}) {
  const headers = { 'Content-Type': 'application/json', 'Accept-Language': locale === 'zh' ? 'zh-CN' : 'en', ...(options.headers || {}) }
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  const init = { ...options, headers }
  if (init.body && typeof init.body !== 'string') init.body = JSON.stringify(init.body)
  let response, text
  try {
    response = await fetch(url, init)
    text = await response.text()
  } catch (e) {
    if (e.name === 'AbortError') throw e
    throw new Error('网络连接失败，请检查网络后重试')
  }
  let data = {}
  try { data = text ? JSON.parse(text) : {} } catch { data = { error: text } }
  if (response.status === 401 && !url.startsWith('/api/auth/') && unauthorizedHandler) {
    await unauthorizedHandler()
  }
  if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`)
  return data
}
