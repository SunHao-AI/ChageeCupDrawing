import { get, post, postForm, request } from './client'
import type {
  Calibration,
  EditApplyResult,
  EditDraft,
  EditProviderInfo,
  ExecParams,
  PlanDetail,
  PlanParams,
  PlanSummary,
  Preset,
  Project,
  Rect,
  TaskCreated,
  TaskSnapshot,
  WindowInfo,
} from './types'

export interface CalibrationPayload {
  hwnd: number
  mode: 'auto' | 'manual'
  rect?: Rect
  captureMethod?: 'maa' | 'win32'
}

export interface TaskCreatePayload {
  exec: ExecParams
  countdownMs: number
  allowDrift: number
}

export const health = () => get<{ status: string; version: string }>('/health')

export const listWindows = () => get<WindowInfo[]>('/windows')

/** 手动校准参考截图的直接地址（作为 <img src>）。 */
export const windowCaptureUrl = (hwnd: number) => `/api/windows/${hwnd}/capture`

export function createProject(file: File, name?: string): Promise<Project> {
  const form = new FormData()
  form.append('file', file)
  if (name) form.append('name', name)
  return postForm<Project>('/projects', form)
}

export const calibrate = (projectId: string, payload: CalibrationPayload) =>
  post<Calibration>(`/projects/${projectId}/calibration`, payload)

export const createPlan = (projectId: string, params: PlanParams, signal?: AbortSignal) =>
  post<PlanSummary>(`/projects/${projectId}/plans`, { params }, signal)

export const getPlan = (planId: string, signal?: AbortSignal) =>
  get<PlanDetail>(`/plans/${planId}`, signal)

export const createTask = (planId: string, payload: TaskCreatePayload) =>
  post<TaskCreated>(`/plans/${planId}/tasks`, payload)

export const getTask = (taskId: string) => get<TaskSnapshot>(`/tasks/${taskId}`)

export const stopTask = (taskId: string) =>
  post<{ taskId: string; status: string }>(`/tasks/${taskId}/stop`)

export const resumeTask = (
  taskId: string,
  payload: { countdownMs: number; allowDrift: number },
) => post<TaskCreated & { cursorPath: number }>(`/tasks/${taskId}/resume`, payload)

export const listPresets = () => get<Preset[]>('/presets')

export const createPreset = (name: string, params: PlanParams) =>
  post<Preset>('/presets', { name, params })

export const lineArtProviders = () => get<unknown[]>('/lineart/providers')

// ---------- VLM 编辑 ----------

export const editProviders = () => get<EditProviderInfo[]>('/edit/providers')

export interface EditPayload {
  instruction: string
  roi?: Rect
  provider?: string
  baseVersion: number
}

export const createEdit = (projectId: string, payload: EditPayload, signal?: AbortSignal) =>
  post<EditDraft>(`/projects/${projectId}/edits`, payload, signal)

export const applyEdit = (editId: string) => post<EditApplyResult>(`/edits/${editId}/apply`, {})

export const discardEdit = (editId: string) =>
  request<{ discarded: string }>(`/edits/${editId}`, { method: 'DELETE' })
