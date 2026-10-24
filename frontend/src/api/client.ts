import { readAiKey } from '@/utils/aiKey'
import type { ApiErrorBody } from './types'

export const API_BASE = '/api'
const CSRF_HEADER = 'X-Requested-With'
const CSRF_VALUE = 'cupdraw'

/** 后端错误体转换而来的结构化异常。 */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly detail?: unknown

  constructor(status: number, body: ApiErrorBody) {
    super(body.message || `请求失败（HTTP ${status}）`)
    this.status = status
    this.code = body.errorCode
    this.detail = body.detail
  }
}

export function isSessionExpired(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401 && error.code === 'E_SESSION_TOKEN'
}

async function parseError(response: Response): Promise<ApiError> {
  try {
    return new ApiError(response.status, (await response.json()) as ApiErrorBody)
  } catch {
    return new ApiError(response.status, {
      errorCode: 'E_INVALID_ARGUMENT',
      message: `请求失败（HTTP ${response.status}）`,
    })
  }
}

interface RequestOptions extends Omit<RequestInit, 'body'> {
  body?: unknown
  form?: FormData
  /** 仅编辑/预检等确实需要模型的请求才携带 Key，避免随普通请求反复外发。 */
  withKey?: boolean
}

function send(path: string, options: RequestOptions): Promise<Response> {
  const { body, form, headers, withKey, ...rest } = options
  const isWrite = rest.method !== undefined && rest.method !== 'GET'
  const finalHeaders = new Headers(headers)
  if (isWrite) finalHeaders.set(CSRF_HEADER, CSRF_VALUE)
  if (body !== undefined) finalHeaders.set('Content-Type', 'application/json')
  if (withKey) {
    const key = readAiKey()
    if (key) finalHeaders.set('X-AI-Key', key)
  }

  return fetch(`${API_BASE}${path}`, {
    ...rest,
    credentials: 'same-origin',
    headers: finalHeaders,
    body: form ?? (body !== undefined ? JSON.stringify(body) : undefined),
  })
}

/** GET 响应会让后端重发会话 Cookie，用于后端重启后的令牌轮换恢复。 */
export async function refreshSession(): Promise<void> {
  await fetch(`${API_BASE}/health`, { credentials: 'same-origin', cache: 'no-store' }).catch(() => undefined)
}

/**
 * 统一请求：同源携带会话 Cookie，写请求补 CSRF 头。
 * 写请求收到 401 E_SESSION_TOKEN 时补领一次 Cookie 并重试一次。
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const isWrite = options.method !== undefined && options.method !== 'GET'

  let response = await send(path, options)
  if (!response.ok && isWrite) {
    const error = await parseError(response)
    if (isSessionExpired(error)) {
      await refreshSession()
      response = await send(path, options)
    } else {
      throw error
    }
  }

  if (!response.ok) throw await parseError(response)
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const get = <T>(path: string, signal?: AbortSignal) => request<T>(path, { signal })

export const post = <T>(path: string, body?: unknown, signal?: AbortSignal) =>
  request<T>(path, { method: 'POST', body, signal })

/** multipart 上传：浏览器自行设置 boundary，不能带 JSON Content-Type。 */
export const postForm = <T>(path: string, form: FormData, signal?: AbortSignal) =>
  request<T>(path, { method: 'POST', form, signal })
