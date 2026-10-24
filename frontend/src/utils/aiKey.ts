/**
 * API Key 只存在本机浏览器：不进后端配置、不落盘、不随普通请求发送。
 * 仅编辑与预检请求通过 X-AI-Key 显式单次携带，服务端用完即弃。
 *
 * 注意：localStorage 对同源脚本可读，需配合 HTTPS 与严格 CSP 使用。
 */
const STORAGE_KEY = 'cupdraw.aiKey'

function storage(): Storage | null {
  try {
    return window.localStorage
  } catch {
    // 隐私模式 / 禁用存储时降级为仅本次会话内存。
    return null
  }
}

let memory = ''

export function readAiKey(): string {
  const store = storage()
  if (!store) return memory
  return store.getItem(STORAGE_KEY) ?? ''
}

export function writeAiKey(value: string): void {
  const trimmed = value.trim()
  const store = storage()
  if (!store) {
    memory = trimmed
    return
  }
  if (trimmed) store.setItem(STORAGE_KEY, trimmed)
  else store.removeItem(STORAGE_KEY)
}

export function clearAiKey(): void {
  writeAiKey('')
}

export function maskAiKey(value: string): string {
  if (!value) return ''
  if (value.length <= 8) return '****'
  return `${value.slice(0, 4)}…${value.slice(-4)}`
}
