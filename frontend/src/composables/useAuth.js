import { reactive } from 'vue'
import { api, setToken, getToken } from '../api/client'

const state = reactive({ user: null, checked: false, allowRegister: true, needsSetup: false })

async function restoreSession() {
  if (!getToken()) {
    state.checked = true
    return null
  }
  try {
    const result = await api('/api/auth/me')
    state.user = result.user
  } catch {
    state.user = null
  } finally {
    state.checked = true
  }
  return state.user
}

async function loadOptions() {
  try {
    const result = await api('/api/auth/options')
    state.allowRegister = result.allow_register
    state.needsSetup = result.needs_setup
  } catch { /* keep default */ }
}

async function login(username, password) {
  const result = await api('/api/auth/login', { method: 'POST', body: { username, password } })
  setToken(result.token)
  state.user = result.user
}

async function register(form) {
  const result = await api('/api/auth/register', { method: 'POST', body: form })
  setToken(result.token)
  state.user = result.user
}

function logout() {
  setToken('')
  state.user = null
}

export function useAuth() {
  return { state, restoreSession, loadOptions, login, register, logout }
}
