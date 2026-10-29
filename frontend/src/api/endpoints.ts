import { get, post, postForm, request } from './client'
import type {
  AiProbeRequest,
  AiSettingsInfo,
  AiSettingsUpdate,
  AiTestResult,
  AutoCalibrationResult,
  Calibration,
  DeviceList,
  EditApplyResult,
  EditDraft,
  EditProviderInfo,
  ExecParams,
  MirrorStatus,
  PlanDetail,
  PlanParams,
  PlanSummary,
  Preset,
  Project,
  Rect,
  RevertResult,
  SourceVersion,
  TaskCreated,
  TaskSnapshot,
} from './types'

export interface CalibrationPayload {
  hwnd: number
  mode: 'auto' | 'manual'
  rect?: Rect
  captureMethod?: 'maa' | 'win32'
  deviceKind?: 'pc' | 'mirror'
  deviceSerial?: string
  slider?: Rect
  undo?: { x: number; y: number }
  fold?: { x: number; y: number }
}

export interface TaskCreatePayload {
  exec: ExecParams
  countdownMs: number
  allowDrift: number
}

export const health = () => get<{ status: string; version: string }>('/health')

/** 设备与投屏目标：PC 窗口 + 已授权安卓设备 + 当前镜像状态。 */
export const listDevices = () => get<DeviceList>('/devices')

export const startMirror = (serial: string) =>
  post<MirrorStatus>('/devices/mirror', { serial })

export const registerMirror = (hwnd: number, serial: string, label = '') =>
  post<MirrorStatus>('/devices/mirror/register', { hwnd, serial, label })

export const stopMirror = () => request<MirrorStatus>('/devices/mirror', { method: 'DELETE' })

/** 手动校准参考截图的直接地址（作为 <img src>）。 */
export const windowCaptureUrl = (hwnd: number) => `/api/windows/${hwnd}/capture`

export function createProject(
  file: File,
  name?: string,
  copyCalibrationFrom?: string | null,
): Promise<Project> {
  const form = new FormData()
  form.append('file', file)
  if (name) form.append('name', name)
  if (copyCalibrationFrom) form.append('copyCalibrationFrom', copyCalibrationFrom)
  return postForm<Project>('/projects', form)
}

export const calibrate = (projectId: string, payload: CalibrationPayload) =>
  post<Calibration>(`/projects/${projectId}/calibration`, payload)

/** I20 一键自动校准：同步执行（可能数十秒），步骤经 openCalibrationEvents 推送。 */
export interface AutoCalibrationPayload {
  hwnd?: number
  confirmDirtyProbe?: boolean
  /** 单独识别范围（canvas/fold/slider/undo/eraser/brush）；缺省或含 "all" 为全量。 */
  targets?: string[]
}

export const autoCalibrate = (projectId: string, payload: AutoCalibrationPayload = {}) =>
  post<AutoCalibrationResult>(`/projects/${projectId}/calibration/auto`, payload)

export const stopAutoCalibrate = (projectId: string) =>
  post<{ projectId: string; stopping: boolean }>(
    `/projects/${projectId}/calibration/auto/stop`,
    {},
  )

/** 校准快照地址（<img src>）：产出当前校准那一帧的 JPEG。 */
export const calibrationSnapshotUrl = (projectId: string, snapshotId: string) =>
  `/api/projects/${projectId}/calibration/snapshot?snapshot_id=${encodeURIComponent(snapshotId)}`

/** 刷新/重开后恢复项目上下文：校准 + 最新规划 + 最新任务（可为空）。 */
export interface ResumeState {
  project: Project
  calibration: Calibration | null
  plan: (PlanSummary & { params: PlanParams }) | null
  task: TaskSnapshot | null
}

export const getResumeState = (projectId: string) =>
  get<ResumeState>(`/projects/${projectId}/resume-state`)

export const createPlan = (projectId: string, params: PlanParams, signal?: AbortSignal) =>
  post<PlanSummary>(`/projects/${projectId}/plans`, { params }, signal)

export const getPlan = (planId: string, signal?: AbortSignal) =>
  get<PlanDetail>(`/plans/${planId}`, signal)

export const createTask = (planId: string, payload: TaskCreatePayload) =>
  post<TaskCreated>(`/plans/${planId}/tasks`, payload)

export const getTask = (taskId: string) => get<TaskSnapshot>(`/tasks/${taskId}`)

export const stopTask = (taskId: string) =>
  post<{ taskId: string; status: string }>(`/tasks/${taskId}/stop`)

/** 删除任务记录，回到「尚未开始绘制」；规划与校准保留（画板墨迹需另行清空）。 */
export const deleteTask = (taskId: string) =>
  request<void>(`/tasks/${taskId}`, { method: 'DELETE' })

/** STOPPED 态回退一笔：点目标画布撤销按钮一次，同步返回墨量对比。 */
export interface UndoResult {
  taskId: string
  effective: boolean
  inkBefore: number
  inkAfter: number
  message: string
}

export const undoTaskStroke = (taskId: string) =>
  post<UndoResult>(`/tasks/${taskId}/undo`)

/** I19 清空画布：连点目标画板撤销按钮直到画布空白（同步执行）。 */
export interface ClearCanvasResult {
  cleared: boolean
  method: 'undo' | 'undo+eraser' | 'already_blank'
  inkBefore: number
  inkAfter: number
  message: string
}

export const clearCanvas = (projectId: string, confirmDirtyProbe = false) =>
  post<ClearCanvasResult>(`/projects/${projectId}/clear-canvas`, { confirmDirtyProbe })

export const resumeTask = (
  taskId: string,
  payload: { countdownMs: number; allowDrift: number },
) => post<TaskCreated & { cursorPath: number }>(`/tasks/${taskId}/resume`, payload)

export const listPresets = () => get<Preset[]>('/presets')

export const createPreset = (name: string, params: PlanParams) =>
  post<Preset>('/presets', { name, params })

export const deletePreset = (presetId: string) =>
  request<void>(`/presets/${presetId}`, { method: 'DELETE' })

export const lineArtProviders = () => get<unknown[]>('/lineart/providers')

// ---------- VLM 编辑 ----------

export const editProviders = () => get<EditProviderInfo[]>('/edit/providers')

export interface EditPayload {
  instruction: string
  roi?: Rect
  provider?: string
  baseVersion: number
  channel?: 'auto' | 'semantic' | 'generative'
}

export const createEdit = (projectId: string, payload: EditPayload, signal?: AbortSignal) =>
  request<EditDraft>(`/projects/${projectId}/edits`, {
    method: 'POST',
    body: payload,
    signal,
    withKey: true,
  })

export const applyEdit = (editId: string) => post<EditApplyResult>(`/edits/${editId}/apply`, {})

export const discardEdit = (editId: string) =>
  request<{ discarded: string }>(`/edits/${editId}`, { method: 'DELETE' })

// ---------- 版本历史与复原（I22） ----------

export const listVersions = (projectId: string) =>
  get<SourceVersion[]>(`/projects/${projectId}/versions`)

export const revertProject = (projectId: string, targetVersion: number) =>
  post<RevertResult>(`/projects/${projectId}/revert`, { targetVersion })

// ---------- AI 设置（I20 / I21） ----------

export const getAiSettings = () => get<AiSettingsInfo>('/settings/ai')

export const putAiSettings = (payload: AiSettingsUpdate) =>
  request<AiSettingsInfo>('/settings/ai', { method: 'PUT', body: payload })

export const testAiSettings = (payload?: AiProbeRequest) =>
  request<AiTestResult>('/settings/ai/test', { method: 'POST', body: payload ?? {}, withKey: true })
