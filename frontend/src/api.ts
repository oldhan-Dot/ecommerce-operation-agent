// 同源 API 客户端：Cookie 由浏览器管理，CSRF 令牌仅保存在内存。
let csrfToken = ''
export function setCsrfToken(value: string) {
  csrfToken = value
}

export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public requestId: string,
  ) {
    super(message)
  }
}

/** 请求统一解包；不在 localStorage 保存登录令牌，不自动重试写请求。 */
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (options.body) headers.set('Content-Type', 'application/json')
  if (csrfToken && !['GET', 'HEAD'].includes(options.method ?? 'GET')) {
    headers.set('X-CSRF-Token', csrfToken)
  }
  let response: Response
  try {
    response = await fetch(`/api/v1${path}`, {
      ...options,
      headers,
      credentials: 'same-origin',
      signal: AbortSignal.timeout(15000),
    })
  } catch {
    throw new ApiError('NETWORK_UNAVAILABLE', '无法连接服务，请检查后端是否启动', '')
  }
  if (response.status === 204) return undefined as T
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    if (response.status === 401 && path !== '/auth/login')
      window.dispatchEvent(new Event('auth:expired'))
    const detail = body.error?.field_errors?.[0]?.message
    throw new ApiError(
      body.error?.code ?? 'REQUEST_FAILED',
      detail ? `${body.error.message}：${detail}` : (body.error?.message ?? '请求失败，请稍后重试'),
      body.request_id ?? '',
    )
  }
  return body.data as T
}

export interface User {
  id: string
  login_name: string
  role: 'ADMIN' | 'SUPERVISOR' | 'OPERATOR'
  status: 'ACTIVE' | 'DISABLED'
  version: number
  store_ids: string[]
  must_change_password: boolean
}
export interface Store {
  id: string
  code: string
  name: string
  platform: string
  status: 'ACTIVE' | 'ARCHIVED'
  version: number
  external_id: string | null
  category: string | null
  notes: string | null
}
export interface Page<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}
export const roles = { ADMIN: '管理员', SUPERVISOR: '运营主管', OPERATOR: '运营人员' }
export const versionHeaders = (version: number) => ({ 'If-Match': `"${version}"` })
export const errorMessage = (e: unknown) =>
  e instanceof Error ? e.message : '操作失败，请稍后重试'
