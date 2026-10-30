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
  minStrokePx: number
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
  focusGuard: boolean
  cursorTakeoverPx: number
  guardPollMs: number
  /** 画布差分守护：投屏目标上每条笔迹结束后比对画布，检出手指误触墨迹。 */
  canvasGuard: boolean
  /** 自动变粗：绘制前自动探测工具条并标定笔宽，按容差逐段换笔。 */
  autoWidth: boolean
  /** 换宽粒度：目标笔宽变化超过该值才换一次笔。 */
  widthTolerance: number
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

/** 绘制目标类型：pc=本机窗口，mirror=手机投屏窗口。 */
export type DeviceKind = 'pc' | 'mirror'

export interface AndroidDevice {
  serial: string
  model: string
}

export interface MirrorStatus {
  serial: string | null
  label: string
  hwnd: number
  running: boolean
  window_title: string
}

export interface DeviceList {
  pcWindows: WindowInfo[]
  android: AndroidDevice[]
  mirror: MirrorStatus
  tools: { available: boolean; missing: string[] }
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
  /** 自动变粗标定：粗细滑杆轨道区域、撤销按钮与面板折叠按钮位置（截图坐标）。 */
  slider?: Rect | null
  undo?: { x: number; y: number } | null
  fold?: { x: number; y: number } | null
  /** 面板展开/折叠两态工具位图（探测产物，结构由后端 executor 保证，原样透传）。 */
  toolsOpen?: Record<string, unknown> | null
  toolsCollapsed?: Record<string, unknown> | null
  /** 一键自动校准快照 ID：标注层据此锚定产出本份校准的那一帧截图。 */
  snapshotId?: string | null
  /** 绘制目标类型与来源设备（mirror 时为手机序列号）。 */
  deviceKind?: DeviceKind
  deviceSerial?: string | null
}

/** 一键自动校准的步骤几何（截图坐标系）：rect 框或 point 圆心。 */
export interface CalibGeometry {
  kind: 'rect' | 'point'
  x: number
  y: number
  w?: number
  h?: number
}

/** RectEditor 只读标注：几何 + 展示文案（截图坐标系）。 */
export interface CalibAnnotation extends CalibGeometry {
  step: string
  label: string
  dashed?: boolean
}

export interface AutoCalibrationResult {
  calibrationId: string | null
  snapshotId: string | null
  rect: Rect
  collapsedCaptured: boolean
  steps: string[]
  message: string
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
  /** 结构化提示码（probe_skipped_dirty / probe_failed），老客户端可忽略。 */
  code?: string
}

export interface TaskDonePayload {
  status: TaskStatus
  stopReason?: string
  /** 守护停笔的判定证据（trigger/偏移/按键读数），用于区分真接管与误判。 */
  stopEvidence?: Record<string, unknown>
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
  cloud: boolean
  needsKey: boolean
}

/** I20 AI 配置（Key 只回掩码）。 */
export interface AiSettingsInfo {
  provider: string | null
  model: string
  /** OpenAI 兼容 / 本地部署的服务地址；留空表示不启用该通道。 */
  baseUrl: string
  apiKeyMasked: string | null
  hasKey: boolean
  keyFromEnv: boolean
  allowCloudEdit: boolean
  envOverridesKey: boolean
}

export interface AiSettingsUpdate {
  provider?: string | null
  model?: string | null
  /** undefined=保持已存 Key；''=清除。 */
  apiKey?: string | null
  baseUrl?: string | null
  allowCloudEdit: boolean
}

export interface AiTestResult {
  ok: boolean
  provider?: string
  reason?: string
}

/** I21 预检可直接带表单值，无需先保存；缺省字段沿用已保存配置。 */
export interface AiProbeRequest {
  provider?: string | null
  model?: string | null
  apiKey?: string | null
  baseUrl?: string | null
  allowCloudEdit?: boolean | null
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

/** I22 源图版本历史项。 */
export interface SourceVersion {
  version: number
  label: string
  current: boolean
  available: boolean
}

/** I22 复原结果：复原内容另存为新版本，版本号只增不减。 */
export interface RevertResult {
  projectId: string
  sourceVersion: number
  parentVersion: number | null
  revertedFrom: number
  imageUrl: string
  width: number
  height: number
}
