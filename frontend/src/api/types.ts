// 与后端 app/api/schemas.py 及 routes_* 响应对齐的类型定义。

export type ImageMode = 'fidelity' | 'adaptive' | 'edge' | 'dither'
export type FitMode = 'contain' | 'cover'
export type Strategy = 'topology' | 'centerline' | 'contour' | 'hscan' | 'tone'
export type CaptureMethod = 'maa' | 'win32'

/** 决定「画什么」，改动须重新规划。 */
export interface PlanParams {
  imageMode: ImageMode
  threshold: number
  contrast: number
  fitMode: FitMode
  lineExpand: number
  thresholdMode: boolean
  autoTrim: boolean
  strategy: Strategy
  brushWidth: number
  simplifyPx: number
  safeMargin: number
  fillDensity: number
  legacyRowStep: number
  supersample: number
  detailKeep: number
  topologyRescue: boolean
  targetRecall: number
  hatchLevels: number
}

/** 决定「怎么画」，可不重新规划直接调整。 */
export interface ExecParams {
  eventStepPx: number
  pointDelayMs: number
  strokeDelayMs: number
  clickHoldMs: number
  noCoalesce: boolean
  shortStrokeProtect: boolean
  shortStrokeThresholdPx: number
}

export interface Rect {
  x: number
  y: number
  w: number
  h: number
}

export interface WindowInfo {
  hwnd: number
  title: string
  className: string
}

export interface Project {
  projectId: string
  name: string
  imageUrl: string
  width: number
  height: number
  /** 源图版本号；应用编辑草稿后递增。 */
  sourceVersion?: number
  createdAt: string
}

export interface Calibration {
  calibrationId: string | null
  rect: Rect | null
  imageWidth: number
  imageHeight: number
  dpiScale: number
  confidence: number
  captureMethod: string
  detectedAt: string
}

export interface PlanMetrics {
  precision: number
  recall: number
  iou: number
  pathCount: number
  totalLength: number
}

export type AssetKind = 'target' | 'simulated' | 'difference' | 'composite'

export interface PlanSummary {
  planId: string
  status: 'DONE'
  strategy: Strategy
  metrics: PlanMetrics
  /** 墨迹占比（0–1）。过高说明整图被误判为墨（如暗调照片 + 全局阈值）。 */
  inkRatio?: number
  /** 实际生效的超采样倍率；源图分辨率不足时后端自动降档。 */
  workScale?: number
  targetSize: { w: number; h: number }
  assets: Record<AssetKind, string>
}

export interface PlanDetail extends PlanSummary {
  params: PlanParams
  createdAt: string
}

export interface Preset {
  presetId: string
  name: string
  params: PlanParams
  builtin: boolean
}

export type TaskStatus =
  | 'PENDING'
  | 'ARMING'
  | 'DRAWING'
  | 'STOPPING'
  | 'DONE'
  | 'STOPPED'
  | 'FAILED'

export const TERMINAL_STATUSES: readonly TaskStatus[] = ['DONE', 'STOPPED', 'FAILED']

export interface TaskCreated {
  taskId: string
  status: TaskStatus
  planId: string
  totalPaths: number
  startedAt: string | null
}

export interface TaskSnapshot {
  taskId: string
  planId: string
  status: TaskStatus
  cursorPath: number
  totalPaths: number
  exec: ExecParams
  stopReason: string | null
  errorCode: string | null
  startedAt: string | null
  finishedAt: string | null
}

export interface TaskProgressPayload {
  donePaths: number
  totalPaths: number
  percent: number
  elapsedMs: number
}

export interface TaskStatePayload {
  status: TaskStatus
  errorCode?: string
  stopReason?: string
}

export interface TaskWarningPayload {
  message: string
}

export interface TaskDonePayload {
  status: TaskStatus
  stopReason?: string
  errorCode?: string
  message?: string
  donePaths: number
  totalPaths: number
  elapsedMs: number
}

/** 后端统一错误体。 */
export interface ApiErrorBody {
  errorCode: string
  message: string
  detail?: unknown
}

// ---------- VLM 编辑（I16–I19） ----------

export type EditChannel = 'semantic' | 'generative'

export interface EditProviderInfo {
  id: string
  label: string
  kind: EditChannel | 'both'
  local: boolean
}

export interface EditOp {
  op: string
  roi?: Rect
  dx?: number
  dy?: number
  factor?: number
  angle?: number
  value?: number
}

export interface EditDraft {
  editId: string
  projectId: string
  channel: EditChannel
  provider: string
  instruction: string
  roi: Rect | null
  baseVersion: number
  draftUrl: string
  diffUrl: string | null
  createdAt: string
  appliedAt: string | null
  parsedOps?: EditOp[] | null
}

export interface EditApplyResult {
  projectId: string
  sourceVersion: number
  parentVersion: number | null
  imageUrl: string
  width: number
  height: number
  edit: EditDraft
}
