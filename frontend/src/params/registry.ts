import type { ExecParams, PlanParams } from '@/api/types'

export type ControlKind = 'slider' | 'segmented' | 'toggle'

export interface FieldBase {
  key: string
  label: string
  kind: ControlKind
  group: string
  hint?: string
  /** 仅当返回 true 时展示（策略相关的条件字段）。 */
  when?: (plan: PlanParams) => boolean
}

export interface SliderField extends FieldBase {
  kind: 'slider'
  min: number
  max: number
  step: number
  /** 展示与提交的小数位。 */
  precision: number
}

export interface SegmentedField extends FieldBase {
  kind: 'segmented'
  options: { value: string; label: string }[]
}

export interface ToggleField extends FieldBase {
  kind: 'toggle'
}

export type FieldMeta = SliderField | SegmentedField | ToggleField

/** PlanParams 全 17 字段，范围/默认与 schemas.py 一致。 */
export const PLAN_GROUPS: { name: string; fields: FieldMeta[] }[] = [
  {
    name: '图像',
    fields: [
      {
        key: 'imageMode',
        label: '图像模式',
        kind: 'segmented',
        group: '图像',
        options: [
          { value: 'fidelity', label: '保真' },
          { value: 'adaptive', label: '自适应' },
          { value: 'edge', label: '边缘' },
          { value: 'dither', label: '抖动' },
        ],
      },
      { key: 'threshold', label: '二值阈值', kind: 'slider', group: '图像', min: 90, max: 230, step: 1, precision: 0 },
      { key: 'contrast', label: '对比度', kind: 'slider', group: '图像', min: 0.6, max: 2.2, step: 0.05, precision: 2 },
      {
        key: 'fitMode',
        label: '适配方式',
        kind: 'segmented',
        group: '图像',
        options: [
          { value: 'contain', label: '完整包含' },
          { value: 'cover', label: '铺满裁切' },
        ],
      },
      { key: 'lineExpand', label: '线宽膨胀', kind: 'slider', group: '图像', min: -2, max: 3, step: 1, precision: 0 },
      { key: 'thresholdMode', label: '纯黑白阈值', kind: 'toggle', group: '图像', hint: '关闭抗锯齿，输出硬边黑白' },
      { key: 'autoTrim', label: '自动裁边', kind: 'toggle', group: '图像' },
      { key: 'supersample', label: '超采样', kind: 'slider', group: '图像', min: 1, max: 4, step: 1, precision: 0 },
      { key: 'detailKeep', label: '细节保留', kind: 'slider', group: '图像', min: 0.05, max: 0.6, step: 0.01, precision: 2 },
    ],
  },
  {
    name: '路径',
    fields: [
      {
        key: 'strategy',
        label: '路径策略',
        kind: 'segmented',
        group: '路径',
        options: [
          { value: 'topology', label: '拓扑填充' },
          { value: 'tone', label: '照片素描' },
          { value: 'centerline', label: '中心线' },
          { value: 'contour', label: '轮廓' },
          { value: 'hscan', label: '水平扫描' },
        ],
      },
      {
        key: 'hatchLevels',
        label: '素描层次',
        kind: 'slider',
        group: '路径',
        min: 6,
        max: 24,
        step: 1,
        precision: 0,
        hint: '照片素描的色调分层数：越大暗部越细腻、笔迹越多',
        when: (p) => p.strategy === 'tone',
      },
      { key: 'brushWidth', label: '笔宽', kind: 'slider', group: '路径', min: 1, max: 12, step: 1, precision: 0 },
      { key: 'simplifyPx', label: '简化精度', kind: 'slider', group: '路径', min: 0.1, max: 2.0, step: 0.02, precision: 2 },
      { key: 'safeMargin', label: '安全边距', kind: 'slider', group: '路径', min: 2, max: 25, step: 1, precision: 0 },
      {
        key: 'fillDensity',
        label: '填充密度',
        kind: 'slider',
        group: '路径',
        min: 0.62,
        max: 1.1,
        step: 0.01,
        precision: 2,
        when: (p) => p.strategy === 'topology',
      },
      {
        key: 'topologyRescue',
        label: '拓扑救援',
        kind: 'toggle',
        group: '路径',
        when: (p) => p.strategy === 'topology',
      },
      {
        key: 'targetRecall',
        label: '目标召回',
        kind: 'slider',
        group: '路径',
        min: 0.95,
        max: 0.998,
        step: 0.001,
        precision: 3,
        when: (p) => p.strategy === 'topology',
      },
      {
        key: 'legacyRowStep',
        label: '扫描行距',
        kind: 'slider',
        group: '路径',
        min: 1,
        max: 20,
        step: 1,
        precision: 0,
        when: (p) => p.strategy === 'hscan',
      },
    ],
  },
]

/** ExecParams 全 7 字段，范围与 ExecParamsModel 一致。 */
export const EXEC_FIELDS: FieldMeta[] = [
  { key: 'eventStepPx', label: '移动事件间隔', kind: 'slider', group: '节奏', min: 0.8, max: 5.0, step: 0.1, precision: 1, hint: '像素/事件，越小越密' },
  { key: 'pointDelayMs', label: '每点延迟', kind: 'slider', group: '节奏', min: 0.5, max: 10.0, step: 0.1, precision: 1, hint: '毫秒，落笔点之间' },
  { key: 'strokeDelayMs', label: '每笔间隔', kind: 'slider', group: '节奏', min: 0, max: 50.0, step: 1, precision: 0, hint: '毫秒，两条笔迹之间' },
  { key: 'clickHoldMs', label: '点击按住', kind: 'slider', group: '节奏', min: 10, max: 200, step: 1, precision: 0, hint: '毫秒，短笔迹按住的时长' },
  { key: 'shortStrokeThresholdPx', label: '短笔迹阈值', kind: 'slider', group: '节奏', min: 0, max: 20, step: 0.2, precision: 1, hint: '低于此长度改走点击' },
  { key: 'noCoalesce', label: '禁止合并移动事件', kind: 'toggle', group: '节奏', hint: 'SendInput NOCOALESCE，长直线不丢中间点' },
  { key: 'shortStrokeProtect', label: '短笔画保护', kind: 'toggle', group: '节奏', hint: '短横/点/撇放慢发送' },
]

/** 创建/续画任务时的控制项。 */
export const TASK_CONTROLS = {
  countdownMs: { min: 0, max: 30000, step: 500, default: 3000 },
  allowDrift: { min: 0, max: 0.5, step: 0.01, precision: 2, default: 0.02 },
} as const

export function defaultPlanParams(): PlanParams {
  return {
    imageMode: 'fidelity',
    threshold: 205,
    contrast: 1.0,
    fitMode: 'contain',
    lineExpand: 0,
    thresholdMode: false,
    autoTrim: true,
    strategy: 'topology',
    brushWidth: 3,
    simplifyPx: 0.16,
    safeMargin: 4,
    fillDensity: 0.82,
    legacyRowStep: 3,
    supersample: 3,
    detailKeep: 0.14,
    topologyRescue: true,
    targetRecall: 0.985,
    hatchLevels: 10,
  }
}

export function defaultExecParams(): ExecParams {
  return {
    eventStepPx: 1.0,
    pointDelayMs: 1.5,
    strokeDelayMs: 6.0,
    clickHoldMs: 20,
    noCoalesce: true,
    shortStrokeProtect: true,
    shortStrokeThresholdPx: 1.2,
  }
}

/** 以补集方式合并入参，保证从预设/导入还原时不丢字段。 */
export function withPlanDefaults(partial: Partial<PlanParams> | undefined): PlanParams {
  return { ...defaultPlanParams(), ...(partial ?? {}) }
}

export function withExecDefaults(partial: Partial<ExecParams> | undefined): ExecParams {
  return { ...defaultExecParams(), ...(partial ?? {}) }
}
