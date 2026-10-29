/** 浏览器会话记忆：上次打开的项目与页签，刷新/重开后自动恢复续画上下文。 */

const PROJECT_KEY = 'cupdraw.lastProject'
const VIEW_KEY = 'cupdraw.view'

export function readLastProjectId(): string | null {
  try {
    return window.localStorage.getItem(PROJECT_KEY)
  } catch {
    return null
  }
}

export function writeLastProjectId(projectId: string | null): void {
  try {
    if (projectId) window.localStorage.setItem(PROJECT_KEY, projectId)
    else window.localStorage.removeItem(PROJECT_KEY)
  } catch {
    // 隐私模式等 localStorage 不可用场景：记忆失效但不影响使用。
  }
}

export function readLastView(): string | null {
  try {
    return window.localStorage.getItem(VIEW_KEY)
  } catch {
    return null
  }
}

export function writeLastView(view: string): void {
  try {
    window.localStorage.setItem(VIEW_KEY, view)
  } catch {
    // 同上
  }
}
