import { defineStore } from 'pinia'
import { computed, shallowRef } from 'vue'
import * as api from '@/api/endpoints'
import { openTaskEvents } from '@/api/sse'
import {
  TERMINAL_STATUSES,
  type TaskProgressPayload,
  type TaskSnapshot,
  type TaskWarningPayload,
} from '@/api/types'
import { errorMessage, toUserError } from '@/utils/error'
import { useParamsStore } from './params'
import { useProjectStore } from './project'

export const useTaskStore = defineStore('task', () => {
  const snapshot = shallowRef<TaskSnapshot | null>(null)
  const progress = shallowRef<TaskProgressPayload | null>(null)
  const warnings = shallowRef<TaskWarningPayload[]>([])
  // ARMING 阶段后端每秒推送的剩余秒数；进入 DRAWING 或任务结束即清空。
  const countdown = shallowRef<number | null>(null)
  const error = shallowRef<string | null>(null)
  const busy = shallowRef(false)
  const streamState = shallowRef<'idle' | 'open' | 'error'>('idle')
  // 暂停态回退：按钮 loading 与「−N px」逐次反馈（用户逐笔目视确认）。
  const undoing = shallowRef(false)
  const undoFeedback = shallowRef<string | null>(null)
  // E_UNDO_UNAVAILABLE 时保留错误码，供运行页在反馈条上挂「去校准页标撤销」按钮。
  const undoFeedbackCode = shallowRef<string | null>(null)
  // 清空画布（I19）：同步接口较慢，单独 loading 与结果反馈。
  const clearing = shallowRef(false)
  const clearFeedback = shallowRef<string | null>(null)

  let closeStream: (() => void) | null = null

  const status = computed(() => snapshot.value?.status ?? null)
  const isActive = computed(() => status.value !== null && !TERMINAL_STATUSES.includes(status.value))
  const canStart = computed(() => status.value === null || TERMINAL_STATUSES.includes(status.value))
  const canStop = computed(() => status.value === 'ARMING' || status.value === 'DRAWING')
  const canResume = computed(() => status.value === 'STOPPED')
  const percent = computed(() => {
    if (progress.value) return progress.value.percent
    const snap = snapshot.value
    if (!snap || snap.totalPaths <= 0) return 0
    return Math.round((snap.cursorPath / snap.totalPaths) * 100)
  })

  function attachStream(taskId: string) {
    closeStream?.()
    // 新建的 EventSource 不带 Last-Event-ID，服务端会从头重放该任务的全部事件。
    // 可重放的状态（warnings / countdown）必须在挂流前清空，让重放成为幂等
    // 重建——否则每次续画/切页都让历史 warning 成倍堆积。
    warnings.value = []
    countdown.value = null
    closeStream = openTaskEvents(taskId, {
      onOpen: () => (streamState.value = 'open'),
      onError: () => (streamState.value = 'error'),
      onState: (payload) => {
        if (snapshot.value) snapshot.value = { ...snapshot.value, status: payload.status }
        if (payload.status !== 'ARMING') countdown.value = null
      },
      onProgress: (payload) => {
        progress.value = payload
        if (snapshot.value) {
          snapshot.value = {
            ...snapshot.value,
            cursorPath: payload.donePaths,
            totalPaths: payload.totalPaths,
          }
        }
      },
      onWarning: (payload) => (warnings.value = [...warnings.value, payload]),
      onCountdown: (payload) => (countdown.value = payload.remainingSec),
      onDone: (payload) => {
        countdown.value = null
        if (snapshot.value) {
          snapshot.value = {
            ...snapshot.value,
            status: payload.status,
            cursorPath: payload.donePaths,
            totalPaths: payload.totalPaths,
            stopReason: payload.stopReason ?? snapshot.value.stopReason,
            errorCode: payload.errorCode ?? snapshot.value.errorCode,
          }
        }
        streamState.value = 'idle'
        closeStream?.()
        closeStream = null
      },
    })
  }

  async function refresh(taskId: string) {
    try {
      const snap = await api.getTask(taskId)
      snapshot.value = snap
      progress.value = null
      if (!TERMINAL_STATUSES.includes(snap.status)) attachStream(taskId)
      return snap
    } catch (e) {
      error.value = errorMessage(e)
      return null
    }
  }

  async function start(planId: string, countdownMs: number, allowDrift: number) {
    busy.value = true
    error.value = null
    warnings.value = []
    progress.value = null
    countdown.value = null
    undoFeedback.value = null
    undoFeedbackCode.value = null
    const projectStore = useProjectStore()
    const epoch = projectStore.currentEpoch()
    try {
      const exec = { ...useParamsStore().exec }
      const created = await api.createTask(planId, { exec, countdownMs, allowDrift })
      // 创建在途时项目已被清除/更换：任务属于旧项目，写回会让它在前端「复活」。
      if (projectStore.currentEpoch() !== epoch) return created
      snapshot.value = {
        taskId: created.taskId,
        planId: created.planId,
        status: created.status,
        cursorPath: 0,
        totalPaths: created.totalPaths,
        exec,
        stopReason: null,
        errorCode: null,
        startedAt: created.startedAt,
        finishedAt: null,
      }
      attachStream(created.taskId)
      return created
    } catch (e) {
      error.value = errorMessage(e)
      throw e
    } finally {
      busy.value = false
    }
  }

  async function resume(countdownMs: number, allowDrift: number) {
    const current = snapshot.value
    if (!current) return
    busy.value = true
    error.value = null
    undoFeedback.value = null
    undoFeedbackCode.value = null
    try {
      await api.resumeTask(current.taskId, { countdownMs, allowDrift })
      await refresh(current.taskId)
    } catch (e) {
      error.value = errorMessage(e)
      throw e
    } finally {
      busy.value = false
    }
  }

  async function stop() {
    const current = snapshot.value
    if (!current) return
    error.value = null
    try {
      const result = await api.stopTask(current.taskId)
      // 以落地时刻的最新快照为底：任务若恰在停止期间自然完成，不能用旧进度覆盖。
      const latest = snapshot.value ?? current
      snapshot.value = { ...latest, status: result.status as TaskSnapshot['status'] }
    } catch (e) {
      error.value = errorMessage(e)
    }
  }

  /** 回退一笔：墨量反馈只报事实（−N px / 无效果），撤几笔由用户逐次决定。 */
  async function undo() {
    const current = snapshot.value
    if (!current || undoing.value) return
    undoing.value = true
    error.value = null
    try {
      const result = await api.undoTaskStroke(current.taskId)
      undoFeedback.value = result.message
      undoFeedbackCode.value = null
    } catch (e) {
      const user = toUserError(e)
      // 无撤销坐标属于「可自助修复」状态，走反馈条 + 引导按钮而不是全局错误。
      if (user.code === 'E_UNDO_UNAVAILABLE') {
        undoFeedback.value = user.message
        undoFeedbackCode.value = user.code
      } else {
        error.value = user.message
        undoFeedbackCode.value = null
      }
    } finally {
      undoing.value = false
    }
  }

  /** 清空画布（用户已确认旧墨全部可弃）。返回是否成功，错误码供视图升级确认。 */
  async function clearCanvas(
    projectId: string,
    confirmDirtyProbe = false,
  ): Promise<{ ok: boolean; code?: string }> {
    if (clearing.value) return { ok: false, code: 'E_BUSY' }
    clearing.value = true
    error.value = null
    clearFeedback.value = null
    try {
      const result = await api.clearCanvas(projectId, confirmDirtyProbe)
      clearFeedback.value = result.message
      return { ok: result.cleared }
    } catch (e) {
      const user = toUserError(e)
      if (user.code === 'E_CLEAR_UNAVAILABLE') {
        clearFeedback.value = user.message
      } else {
        error.value = user.message
      }
      return { ok: false, code: user.code }
    } finally {
      clearing.value = false
    }
  }

  /** 删除任务记录（终态后才允许），回到「尚未开始绘制」的初始状态。 */
  async function remove() {
    const current = snapshot.value
    if (!current || isActive.value || busy.value) return false
    busy.value = true
    error.value = null
    try {
      await api.deleteTask(current.taskId)
      detach()
      snapshot.value = null
      progress.value = null
      warnings.value = []
      undoFeedback.value = null
      undoFeedbackCode.value = null
      clearFeedback.value = null
      return true
    } catch (e) {
      error.value = errorMessage(e)
      return false
    } finally {
      busy.value = false
    }
  }

  /** 刷新后恢复上次任务快照：未完成（STOPPED/进行中）则接上快照与事件流供续画。 */
  async function restore(snap: TaskSnapshot) {
    snapshot.value = snap
    if (!TERMINAL_STATUSES.includes(snap.status)) await refresh(snap.taskId)
  }

  function detach() {
    closeStream?.()
    closeStream = null
    streamState.value = 'idle'
  }

  /** 无条件清空本地任务状态（不请求后端）；「清除作品」组合清理收尾用。 */
  function clearLocal() {
    detach()
    snapshot.value = null
    progress.value = null
    warnings.value = []
    countdown.value = null
    error.value = null
    undoing.value = false
    undoFeedback.value = null
    undoFeedbackCode.value = null
    clearFeedback.value = null
  }

  /** 视图切回时若任务仍在进行，重新接上事件流。 */
  function ensureStream() {
    const snap = snapshot.value
    if (!snap || TERMINAL_STATUSES.includes(snap.status)) return
    if (closeStream && streamState.value === 'open') return
    refresh(snap.taskId)
  }

  return {
    snapshot,
    progress,
    warnings,
    countdown,
    error,
    busy,
    undoing,
    undoFeedback,
    undoFeedbackCode,
    clearing,
    clearFeedback,
    streamState,
    status,
    isActive,
    canStart,
    canStop,
    canResume,
    percent,
    refresh,
    start,
    resume,
    stop,
    undo,
    clearCanvas,
    remove,
    restore,
    detach,
    clearLocal,
    ensureStream,
  }
})
