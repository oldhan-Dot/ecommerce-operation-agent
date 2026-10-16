// 登录态只存在内存中；刷新后向服务端恢复当前身份与最新店铺权限。
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api, ApiError, setCsrfToken, type User } from '../api'

interface SessionInfo {
  user: User
  csrf_token: string
  expires_at: string
}

export const useAuth = defineStore('auth', () => {
  const user = ref<User | null>(null)
  const initialized = ref(false)
  const connectionError = ref('')

  function accept(data: SessionInfo) {
    user.value = data.user
    setCsrfToken(data.csrf_token)
    initialized.value = true
    connectionError.value = ''
  }
  function clear() {
    user.value = null
    setCsrfToken('')
    initialized.value = true
  }
  async function restore() {
    try {
      accept(await api<SessionInfo>('/auth/session'))
    } catch (e) {
      clear()
      if (!(e instanceof ApiError && e.code === 'AUTH_SESSION_EXPIRED')) {
        connectionError.value = e instanceof Error ? e.message : '服务暂不可用'
      }
    }
  }
  async function login(login_name: string, password: string) {
    accept(
      await api<SessionInfo>('/auth/login', {
        method: 'POST',
        body: JSON.stringify({ login_name, password }),
      }),
    )
  }
  async function logout() {
    await api('/auth/logout', { method: 'POST' })
    clear()
  }
  return { user, initialized, connectionError, restore, login, logout, clear }
})
