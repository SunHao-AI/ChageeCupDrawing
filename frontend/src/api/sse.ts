import type {
  CalibGeometry,
  TaskDonePayload,
  TaskProgressPayload,
  TaskStatePayload,
  TaskWarningPayload,
} from './types'

export interface TaskEventHandlers {
  onState?: (payload: TaskStatePayload) => void
  onProgress?: (payload: TaskProgressPayload) => void
  onWarning?: (payload: TaskWarningPayload) => void
  onCountdown?: (payload: { remainingSec: number }) => void
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
  bind<{ remainingSec: number }>('task.countdown', handlers.onCountdown)
  bind<TaskDonePayload>('task.done', handlers.onDone)

  return () => source.close()
}

export interface CalibrationStepPayload {
  message: string
  step: string
  state: string
}

export interface CalibrationGeometryPayload {
  step: string
  geometry: CalibGeometry
}

export interface CalibrationEventHandlers {
  /** 步骤进度/失败文本（calibration.warning）。 */
  onWarning?: (payload: CalibrationStepPayload) => void
  /** 某步骤识别成功的几何（用于快照上的实时标注）。 */
  onGeometry?: (payload: CalibrationGeometryPayload) => void
  onDone?: (payload: Record<string, unknown>) => void
  /**
   * 终态失败。``saved`` 为真表示成功项已落库（部分成功）：``savedSteps`` 是
   * 已入库步骤，``failedSteps`` 是本轮未学到的步骤，前端据此预选「单独识别」
   * 让用户只补失败项。
   */
  onFailed?: (payload: {
    code: string
    message: string
    saved?: boolean
    savedSteps?: string[]
    failedSteps?: string[]
  }) => void
  onError?: (error: Event) => void
}

/**
 * 一键自动校准的 SSE 步骤流。POST 同步返回结果，事件流只负责过程展示；
 * 服务端在 run 终态后保留一小段窗口补尾再结束流。
 */
export function openCalibrationEvents(
  projectId: string,
  handlers: CalibrationEventHandlers,
): () => void {
  const source = new EventSource(`/api/projects/${projectId}/calibration/events`, {
    withCredentials: true,
  })

  const bind = <T,>(type: string, cb?: (payload: T) => void) => {
    if (!cb) return
    source.addEventListener(type, (event: MessageEvent) => {
      cb(JSON.parse(event.data) as T)
    })
  }

  source.onerror = (event) => handlers.onError?.(event)
  bind<CalibrationStepPayload>('calibration.warning', handlers.onWarning)
  bind<CalibrationGeometryPayload>('calibration.geometry', handlers.onGeometry)
  bind<Record<string, unknown>>('calibration.done', handlers.onDone)
  bind<{ code: string; message: string }>('calibration.failed', handlers.onFailed)

  return () => source.close()
}
