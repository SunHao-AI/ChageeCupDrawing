import { ApiError } from '@/api/client'

export interface UserError {
  code: string
  message: string
}

export function toUserError(error: unknown): UserError {
  if (error instanceof ApiError) {
    return { code: error.code, message: error.message }
  }
  if (error instanceof DOMException && error.name === 'AbortError') {
    return { code: 'E_ABORTED', message: '请求已取消' }
  }
  if (error instanceof TypeError) {
    return { code: 'E_NETWORK', message: '无法连接本地服务，请确认后端已启动' }
  }
  return { code: 'E_UNKNOWN', message: error instanceof Error ? error.message : String(error) }
}

export function errorMessage(error: unknown): string {
  return toUserError(error).message
}
