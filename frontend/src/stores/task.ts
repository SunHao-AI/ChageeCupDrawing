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
import { errorMessage } from '@/utils/error'
import { useParamsStore } from './params'

export const useTaskStore = defineStore('task', () => {
  const snapshot = shallowRef<TaskSnapshot | null>(null)
  const progress = shallowRef<TaskProgressPayload | null>(null)
  const warnings = shallowRef<TaskWarningPayload[]>([])
  const error = shallowRef<string | null>(null)
  const busy = shallowRef(false)
  const streamState = shallowRef<'idle' | 'open' | 'error'>('idle')

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
    closeStream = openTaskEvents(taskId, {
      onOpen: () => (streamState.value = 'open'),
      onError: () => (streamState.value = 'error'),
      onState: (payload) => {
        if (snapshot.value) snapshot.value = { ...snapshot.value, status: payload.status }
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
      onDone: (payload) => {
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
    try {
      const exec = { ...useParamsStore().exec }
      const created = await api.createTask(planId, { exec, countdownMs, allowDrift })
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
      snapshot.value = { ...current, status: result.status as TaskSnapshot['status'] }
    } catch (e) {
      error.value = errorMessage(e)
    }
  }

  function detach() {
    closeStream?.()
    closeStream = null
    streamState.value = 'idle'
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
    error,
    busy,
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
    detach,
    ensureStream,
  }
})
