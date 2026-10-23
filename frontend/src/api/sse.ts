import type {
  TaskDonePayload,
  TaskProgressPayload,
  TaskStatePayload,
  TaskWarningPayload,
} from './types'

export interface TaskEventHandlers {
  onState?: (payload: TaskStatePayload) => void
  onProgress?: (payload: TaskProgressPayload) => void
  onWarning?: (payload: TaskWarningPayload) => void
  onDone?: (payload: TaskDonePayload) => void
  onError?: (error: Event) => void
  onOpen?: () => void
}

/**
 * 任务事件流。EventSource 自动重连并携带 Last-Event-ID，
 * 服务端从 task_event 表补发，因此不丢也不重。终态后服务端主动结束流。
 */
export function openTaskEvents(taskId: string, handlers: TaskEventHandlers): () => void {
  const source = new EventSource(`/api/tasks/${taskId}/events`, { withCredentials: true })

  const bind = <T,>(type: string, cb?: (payload: T) => void) => {
    if (!cb) return
    source.addEventListener(type, (event: MessageEvent) => {
      cb(JSON.parse(event.data) as T)
    })
  }

  source.onopen = () => handlers.onOpen?.()
  source.onerror = (event) => handlers.onError?.(event)
  bind<TaskStatePayload>('task.state', handlers.onState)
  bind<TaskProgressPayload>('task.progress', handlers.onProgress)
  bind<TaskWarningPayload>('task.warning', handlers.onWarning)
  bind<TaskDonePayload>('task.done', handlers.onDone)

  return () => source.close()
}
