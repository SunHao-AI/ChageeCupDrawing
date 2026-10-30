<script setup lang="ts">
import { computed, onMounted, onUnmounted, shallowRef, watch } from 'vue'
import {
  autoCalibrate,
  calibrationSnapshotUrl,
  getResumeState,
  stopAutoCalibrate,
  windowCaptureUrl,
} from '@/api/endpoints'
import { openCalibrationEvents, type CalibrationStepPayload } from '@/api/sse'
import CalibDiagram from '@/components/CalibDiagram.vue'
import ErrorBanner from '@/components/ErrorBanner.vue'
import GuideCard from '@/components/GuideCard.vue'
import RectEditor from '@/components/RectEditor.vue'
import { ApiError } from '@/api/client'
import { TARGET_WINDOW_KEYWORD, useProjectStore } from '@/stores/project'
import type { CalibAnnotation, CalibGeometry, Rect } from '@/api/types'
import type { ViewEmits } from './types'

const emit = defineEmits<ViewEmits>()
const projectStore = useProjectStore()

const captureNonce = shallowRef(0)
const manualRect = shallowRef<Rect | null>(null)
const lastConfidence = shallowRef<number | null>(null)
const notDetected = shallowRef(false)
// 首次校准前 calibration 为空，用截图自身加载出的自然尺寸做框选坐标基准。
const naturalSize = shallowRef({ w: 0, h: 0 })

// 框选目标四模式：面板折叠按钮 / 笔粗细滑杆轨道 / 撤销按钮 / 画布（自动变粗标定用）。
type CalibTarget = 'fold' | 'slider' | 'undo' | 'canvas'
const calibTarget = shallowRef<CalibTarget>('canvas')
const sliderRect = shallowRef<Rect | null>(null)
const undoRect = shallowRef<Rect | null>(null)
const foldRect = shallowRef<Rect | null>(null)

const STEP_LABELS: Record<string, string> = {
  canvas: '画布区域',
  brush_entry: '画笔入口',
  undo: '撤销按钮',
  fold: '折叠按钮',
  slider: '滑杆轨道',
  eraser: '橡皮',
  brush: '画笔',
  fingerprint: '画笔指纹',
  cleanup: '画布清理',
  collapsed: '收起态重扫',
}

// 运行页引导卡「去校准页补标 ×」跳来时直接切到对应框选模式（一次性）。
onMounted(() => {
  const mark = projectStore.takeCalibMark()
  if (mark) calibTarget.value = mark
})

const editorRect = computed({
  get: () =>
    calibTarget.value === 'canvas'
      ? manualRect.value
      : calibTarget.value === 'slider'
        ? sliderRect.value
        : calibTarget.value === 'undo'
          ? undoRect.value
          : foldRect.value,
  set: (rect) => {
    if (calibTarget.value === 'canvas') manualRect.value = rect
    else if (calibTarget.value === 'slider') sliderRect.value = rect
    else if (calibTarget.value === 'undo') undoRect.value = rect
    else foldRect.value = rect
  },
})

const editorHint = computed(() =>
  calibTarget.value === 'canvas'
    ? '在截图上按住拖动，框出白色杯贴画布区域'
    : calibTarget.value === 'slider'
      ? '先点画板工具行最左「∧/∨」展开面板，再框住下方「大小」滑杆的整条轨道（贴轨道两端，不含圆点大小）'
      : calibTarget.value === 'undo'
        ? '点一下或框住「↩」撤销胶囊的中心：它在开合钮正上方、起于画布左缘之外（标定测试线自动清空用）'
        : '点一下工具行最左的开合钮中心（在撤销胶囊正下方）：收起态是「∨」（点开），展开态是「∧」（点收）——同一位置、功能相反的两个按钮',
)

const captureSrc = computed(() =>
  projectStore.selectedHwnd === null
    ? ''
    : `${windowCaptureUrl(projectStore.selectedHwnd)}?n=${captureNonce.value}`,
)

const imageSize = computed(() => {
  const cal = projectStore.calibration
  if (cal && cal.imageWidth > 0) return { w: cal.imageWidth, h: cal.imageHeight }
  return naturalSize.value
})

function onCaptureLoad(event: Event) {
  const img = event.target as HTMLImageElement
  naturalSize.value = { w: img.naturalWidth, h: img.naturalHeight }
}

const rectText = computed(() => {
  const rect = projectStore.calibration?.rect
  return rect ? `x ${rect.x} · y ${rect.y} · ${rect.w}×${rect.h}` : '—'
})

// 自动识别成功后把手动框选对齐到已识别矩形，方便微调。
watch(
  () => projectStore.calibration?.rect,
  (rect) => {
    if (rect) manualRect.value = { ...rect }
  },
)

// 默认选中目标窗口（霸王茶姬窗口或已登记投屏窗口）后自动跑一次识别；
// 用户手动换窗口或已有校准结果时不触发，避免覆盖手动校准。
const autoRan = shallowRef(false)
watch(
  () => projectStore.selectedHwnd,
  (hwnd) => {
    if (autoRan.value || hwnd === null) return
    const isTarget =
      projectStore.isMirrorTarget || hwnd === projectStore.targetWindow?.hwnd
    if (!isTarget) return
    autoRan.value = true
    if (!projectStore.calibrated) onAuto()
  },
  { immediate: true },
)

function refreshCapture() {
  captureNonce.value += 1
  liveMode.value = true
}

// 目标在 PC 窗口与投屏窗口之间切换时，旧校准的坐标系已失效，自动重新识别一次。
// 前提是项目已带校准（刷新恢复的项目先于窗口列表到达时不该凭空重识别）。
watch(
  () => projectStore.deviceKind,
  () => {
    autoRan.value = false
    if (projectStore.selectedHwnd !== null && projectStore.calibrated) {
      autoRan.value = true
      onAuto()
    }
  },
)

// 后端截图前会自动把目标窗口带到前台（遮挡/最小化均可恢复），无需再手动切窗口。
async function onAuto() {
  notDetected.value = false
  const result = await projectStore.runCalibration('auto')
  if (!result) return
  lastConfidence.value = result.confidence
  notDetected.value = result.rect === null
}

async function onManual() {
  const rect = manualRect.value
  if (!rect || rect.w < 4 || rect.h < 4) return
  const result = await projectStore.runCalibration('manual', rect, extrasPayload())
  if (result) lastConfidence.value = result.confidence
}

/** 提交时始终附带已有的滑杆/撤销/折叠标记，避免互相覆盖。 */
function extrasPayload() {
  const cal = projectStore.calibration
  const slider = sliderRect.value ?? cal?.slider ?? undefined
  const center = (value: Rect | { x: number; y: number } | null | undefined) => {
    if (!value) return undefined
    return 'w' in value
      ? { x: value.x + Math.floor(value.w / 2), y: value.y + Math.floor(value.h / 2) }
      : value
  }
  return {
    slider: slider ?? undefined,
    undo: center(undoRect.value ?? cal?.undo ?? null),
    fold: center(foldRect.value ?? cal?.fold ?? null),
  }
}

/** 滑杆/撤销标记的独立保存（沿用已识别的画布矩形）。 */
async function onSaveExtras() {
  const cal = projectStore.calibration
  if (!cal?.rect) return
  const result = await projectStore.runCalibration('manual', cal.rect, extrasPayload())
  if (result) lastConfidence.value = result.confidence
}

// ---------- 一键自动校准 ----------

const autoRunning = shallowRef(false)
const autoSteps = shallowRef<CalibrationStepPayload[]>([])
const liveGeometry = shallowRef<Record<string, CalibGeometry>>({})
const autoNote = shallowRef<string | null>(null)
const dirtyAck = shallowRef(false)
let closeEvents: (() => void) | null = null
// 事件流世代号：重跑校准后旧流的回调一律作废。
let streamEpoch = 0
// 「延迟关流」只许关闭创建它的那一路流：重跑后旧定时器不得误杀新流。
let closeTimer: ReturnType<typeof setTimeout> | null = null
// 部分成功落库只应用一次（POST 错误体与 SSE failed 携同一事实）。
let partialHandled = false

function scheduleStreamClose(delayMs: number) {
  if (closeTimer !== null) clearTimeout(closeTimer)
  const target = closeEvents
  closeTimer = setTimeout(() => {
    closeTimer = null
    if (target && closeEvents === target) {
      target()
      closeEvents = null
    }
  }, delayMs)
}

/** 单独识别勾选集（空 = 全量）。滑杆必带折叠（轨道由折叠流程顺带产出）。 */
const pickTargets = shallowRef<Set<string>>(new Set())
const TARGET_CHOICES = [
  { id: 'canvas', label: '画布' },
  { id: 'undo', label: '撤销' },
  { id: 'fold', label: '折叠钮' },
  { id: 'slider', label: '滑杆' },
  { id: 'eraser', label: '橡皮' },
  { id: 'brush', label: '画笔' },
]

function toggleTarget(id: string) {
  const next = new Set(pickTargets.value)
  if (next.has(id)) next.delete(id)
  else {
    next.add(id)
    if (id === 'slider') next.add('fold')
  }
  pickTargets.value = next
}

function stepIndex(step: string): number {
  return Object.keys(STEP_LABELS).indexOf(step)
}

function pushStep(payload: CalibrationStepPayload) {
  const steps = autoSteps.value
  const index = steps.findIndex((item) => item.step === payload.step)
  if (index >= 0) steps.splice(index, 1, payload)
  else steps.push(payload)
  autoSteps.value = [...steps].sort(
    (a, b) => (stepIndex(a.step) - stepIndex(b.step)) || 0,
  )
}

function startEventStream() {
  const projectId = projectStore.project?.projectId
  if (!projectId) return
  closeEvents?.()
  const epoch = ++streamEpoch
  closeEvents = openCalibrationEvents(projectId, {
    // 只认本世代事件流：上一轮校准的迟到回调不得改写新一轮的步骤流与提示。
    onWarning: (payload) => epoch === streamEpoch && pushStep(payload),
    onGeometry: (payload) => {
      if (epoch === streamEpoch) {
        liveGeometry.value = { ...liveGeometry.value, [payload.step]: payload.geometry }
      }
    },
    onFailed: (payload) => {
      if (epoch !== streamEpoch) return
      if (payload.saved) {
        applyPartialSaved(payload.message, payload.failedSteps)
      } else {
        autoNote.value = payload.message
      }
    },
  })
}

/**
 * 部分成功落库：成功项（含画布）已入库，把本轮失败项预选进「单独识别」，
 * 用户直接再点一次就只补失败项；回读校准让标注层立即显示已保存成果。
 */
function applyPartialSaved(message: string, failedSteps?: string[]) {
  // POST 错误体与 SSE calibration.failed 携同一事实，先到先得、只应用一次。
  if (partialHandled) return
  partialHandled = true
  const failed = (failedSteps ?? []).filter((id) =>
    TARGET_CHOICES.some((choice) => choice.id === id),
  )
  if (failed.length) pickTargets.value = new Set(failed)
  autoNote.value =
    `${message} —— 成功项已保存，已勾选失败项，再次点击「一键自动校准」只补失败项`
  void refreshCalibration()
  captureNonce.value += 1
}

function stopEventStream() {
  streamEpoch += 1
  closeEvents?.()
  closeEvents = null
}

/** 校准几何 → RectEditor 只读标注（后端 step 名与前端控件同词表）。 */
function geometryToAnnotation(
  step: string,
  geometry: CalibGeometry,
  options?: { dashed?: boolean; suffix?: string },
): CalibAnnotation {
  return {
    step,
    kind: geometry.kind,
    x: geometry.x,
    y: geometry.y,
    w: geometry.w,
    h: geometry.h,
    label: (STEP_LABELS[step] ?? step) + (options?.suffix ?? ''),
    dashed: options?.dashed,
  }
}

const cal = computed(() => projectStore.calibration)

/** 用户点「刷新截图」后切回实时帧；自动校准结束后再切回快照帧。 */
const liveMode = shallowRef(false)

/** 标注底图：优先校准快照帧（标注与像素严格同帧），无快照回落实时截图。 */
const baseSrc = computed(() => {
  const current = cal.value
  if (!liveMode.value && !autoRunning.value && current?.snapshotId && current.rect && projectStore.project) {
    return calibrationSnapshotUrl(projectStore.project.projectId, current.snapshotId)
  }
  return captureSrc.value
})

/** 帧不符：底图是实时截图而标注来自快照帧（或压根没快照）——标注淡出。 */
const annotationsStale = computed(
  () =>
    Boolean(cal.value?.rect) &&
    autoSteps.value.length === 0 &&
    (!cal.value?.snapshotId || liveMode.value),
)

const finalAnnotations = computed<CalibAnnotation[]>(() => {
  const current = cal.value
  if (!current?.rect || autoRunning.value) return []
  const items: CalibAnnotation[] = [geometryToAnnotation('canvas', { kind: 'rect', ...current.rect })]
  if (current.slider) items.push(geometryToAnnotation('slider', { kind: 'rect', ...current.slider }))
  if (current.undo) items.push(geometryToAnnotation('undo', { kind: 'point', ...current.undo }))
  if (current.fold) items.push(geometryToAnnotation('fold', { kind: 'point', ...current.fold }))
  const open = current.toolsOpen as
    | { brush?: [number, number]; eraser?: [number, number] }
    | null
    | undefined
  if (open?.brush) items.push(geometryToAnnotation('brush', { kind: 'point', x: open.brush[0], y: open.brush[1] }))
  if (open?.eraser) items.push(geometryToAnnotation('eraser', { kind: 'point', x: open.eraser[0], y: open.eraser[1] }))
  const collapsed = current.toolsCollapsed as {
    canvas_rect?: [number, number, number, number]
    fold?: [number, number]
    undo?: [number, number]
    eraser?: [number, number]
    brush?: [number, number]
  } | null | undefined
  if (collapsed?.canvas_rect) {
    const [x, y, w, h] = collapsed.canvas_rect
    items.push(
      geometryToAnnotation('canvas', { kind: 'rect', x, y, w, h }, { dashed: true, suffix: '(收)' }),
    )
  }
  for (const name of ['fold', 'undo', 'eraser', 'brush'] as const) {
    const point = collapsed?.[name]
    if (point) {
      // 「折叠」「展开」是两个独立按钮：收起态位图的 fold 存的是收起态生效的
      // 「展开」钮，标注语义要与实际点击的控件一致。
      const label = name === 'fold' ? '展开按钮' : STEP_LABELS[name] ?? name
      items.push({
        step: name,
        kind: 'point',
        x: point[0],
        y: point[1],
        label: label + '(收)',
        dashed: true,
      })
    }
  }
  return items
})

/** 运行中显示实时几何，结束后显示落库结果。 */
const editorAnnotations = computed<CalibAnnotation[]>(() => {
  if (autoRunning.value) {
    return Object.entries(liveGeometry.value).map(([step, geometry]) =>
      geometryToAnnotation(step, geometry),
    )
  }
  return finalAnnotations.value
})

const editorAnnotationsDim = computed(
  () => !autoRunning.value && annotationsStale.value,
)

function stepIcon(state: string): string {
  if (state === 'done') return '✓'
  if (state === 'failed') return '✕'
  if (state === 'running') return '…'
  return '·'
}

async function refreshCalibration() {
  const projectId = projectStore.project?.projectId
  if (!projectId) return
  try {
    const state = await getResumeState(projectId)
    projectStore.calibration = state.calibration
  } catch {
    /* 回读失败不影响本次结果展示 */
  }
}

async function onRunAuto() {
  const projectId = projectStore.project?.projectId
  if (!projectId || autoRunning.value) return
  autoRunning.value = true
  autoSteps.value = []
  liveGeometry.value = {}
  autoNote.value = null
  partialHandled = false
  projectStore.error = null
  startEventStream()
  try {
    const targets = pickTargets.value.size ? [...pickTargets.value] : undefined
    const result = await autoCalibrate(projectId, {
      hwnd: projectStore.selectedHwnd ?? undefined,
      confirmDirtyProbe: dirtyAck.value,
      targets,
    })
    autoNote.value = result.collapsedCaptured
      ? '自动校准完成（已捕获收起态工具位图）'
      : '自动校准完成（收起态重扫未成功，收起态坐标沿用旧值）'
    await refreshCalibration()
    if (result.rect) manualRect.value = { ...result.rect }
    captureNonce.value += 1
  } catch (e) {
    if (e instanceof ApiError) {
      const detail = e.detail as { saved?: boolean; failedSteps?: string[] } | undefined
      if (detail?.saved) {
        applyPartialSaved(e.message, detail.failedSteps)
      } else {
        autoNote.value = e.message
      }
      if (e.code === 'E_CANVAS_DIRTY') dirtyAck.value = true
    } else {
      autoNote.value = String(e)
    }
  } finally {
    autoRunning.value = false
    // 终态事件由服务端保留窗口内补发，稍后再关流，保证尾步可见。
    scheduleStreamClose(1200)
  }
}

function onStopAuto() {
  const projectId = projectStore.project?.projectId
  if (projectId) stopAutoCalibrate(projectId).catch(() => {})
}

onUnmounted(stopEventStream)

const primaryDisabled = computed(() => {
  if (projectStore.busy) return true
  if (calibTarget.value === 'canvas') {
    const rect = manualRect.value
    return !rect || rect.w < 4 || rect.h < 4
  }
  if (calibTarget.value === 'slider') return !sliderRect.value || sliderRect.value.w < 10
  if (calibTarget.value === 'undo') return !undoRect.value
  return !foldRect.value
})

const primaryLabel = computed(() =>
  calibTarget.value === 'canvas' ? '应用手动框选' : '保存该标记',
)

function goStudio() {
  emit('goto', 'studio')
}

/** 目标窗口提示：投屏目标 / 未找到 / 已选中 / 选了别的窗口，四种口吻不同。 */
const targetTip = computed(() => {
  if (projectStore.isMirrorTarget) {
    return {
      tone: 'info' as const,
      text: `绘制目标为手机投屏窗口（${projectStore.mirror?.label ?? ''}）。请保持手机停留在小程序画板页、投屏窗口可见不被遮挡。`,
    }
  }
  const target = projectStore.targetWindow
  if (!target) {
    return { tone: 'warn' as const, text: `未找到「${TARGET_WINDOW_KEYWORD}」小程序窗口。请先在电脑上打开小程序并进入画板页，再点「重新枚举」；或在下方连接手机投屏。` }
  }
  if (projectStore.selectedHwnd === target.hwnd) {
    return { tone: 'info' as const, text: `绘制目标为「${TARGET_WINDOW_KEYWORD}」小程序窗口（已自动选中），请保持其可见。` }
  }
  return { tone: 'warn' as const, text: `当前选中的不是「${TARGET_WINDOW_KEYWORD}」小程序窗口，绘制只会落笔到所选窗口，请确认。` }
})

async function onStartMirror(serial: string) {
  try {
    await projectStore.startMirror(serial)
  } catch {
    /* 错误文案已写入 store.error，由 ErrorBanner 展示 */
  }
}

function onRegisterSelected() {
  const hwnd = projectStore.selectedHwnd
  if (hwnd === null) return
  const title = projectStore.selectedWindow?.title || `窗口 ${hwnd}`
  projectStore.registerMirrorWindow(hwnd, `window-${hwnd}`, title).catch(() => {})
}
</script>

<template>
  <div v-if="!projectStore.project" class="empty">
    <p>校准前需要先导入原图。</p>
    <button class="btn btn-primary" @click="goStudio">去工作台导入图片</button>
  </div>

  <div v-else class="split layout" style="--split-side: 300px">
    <section class="card side">
      <h3 class="card-title">目标窗口</h3>
      <div class="field">
        <div class="field-label"><span>选择窗口</span></div>
        <select v-model="projectStore.selectedHwnd" class="select" @change="projectStore.userPicked = true">
          <option :value="null" disabled>请选择</option>
          <option v-for="win in projectStore.windows" :key="win.hwnd" :value="win.hwnd">
            {{ win.title || '(无标题)' }}
          </option>
        </select>
      </div>
      <div class="banner target-tip" :class="targetTip.tone === 'warn' ? 'banner-warn' : 'banner-info'">
        {{ targetTip.text }}
      </div>
      <button class="btn btn-sm" :disabled="projectStore.busy" @click="projectStore.refreshWindows()">
        重新枚举
      </button>

      <div class="divider"></div>
      <h3 class="card-title">手机投屏目标</h3>
      <template v-if="projectStore.mirror?.running">
        <div class="banner banner-info target-tip">
          已连接：{{ projectStore.mirror.label }}（窗口 {{ projectStore.mirror.hwnd }}）
        </div>
        <button class="btn btn-sm" :disabled="projectStore.busy" @click="projectStore.unregisterMirror()">
          断开投屏目标
        </button>
      </template>
      <template v-else-if="projectStore.android.length">
        <div class="field-label"><span>已授权设备</span></div>
        <button
          v-for="dev in projectStore.android"
          :key="dev.serial"
          class="btn btn-sm mirror-btn"
          :disabled="projectStore.busy"
          @click="onStartMirror(dev.serial)"
        >
          投屏 {{ dev.model || dev.serial }}
        </button>
      </template>
      <p v-else class="tip">
        {{ projectStore.tools.available
          ? '未检测到已授权的安卓设备。请 USB 连接手机并开启调试。'
          : `未安装投屏工具（${projectStore.tools.missing.join('、')}）。安装后即可把手机画面作为绘制目标；也可手工启动 scrcpy 后在上方窗口列表直接选择其窗口。` }}
      </p>
      <button
        v-if="!projectStore.mirror?.running && projectStore.selectedHwnd !== null"
        class="btn btn-sm"
        :disabled="projectStore.busy"
        title="把上方选中的窗口当作投屏目标登记（接入外部启动的 scrcpy，或以设备模拟窗口验证链路）"
        @click="onRegisterSelected"
      >
        登记选中窗口为投屏目标
      </button>

      <div class="divider"></div>
      <h3 class="card-title">识别结果</h3>
      <dl class="meta">
        <div><dt>画布矩形</dt><dd class="num">{{ rectText }}</dd></div>
        <div><dt>截图尺寸</dt><dd class="num">{{ imageSize.w }}×{{ imageSize.h }}</dd></div>
        <div><dt>DPI 缩放</dt><dd class="num">{{ projectStore.calibration?.dpiScale ?? '—' }}</dd></div>
        <div><dt>置信度</dt><dd class="num">{{ lastConfidence ?? '—' }}</dd></div>
      </dl>

      <ErrorBanner v-if="projectStore.error" kind="error">{{ projectStore.error }}</ErrorBanner>
      <div v-if="notDetected" class="banner banner-warn">
        未识别到合格画布。请确认小程序画布为白底后重试「自动识别」，或在右侧手动框选。
      </div>
      <GuideCard
        v-if="projectStore.calibrated"
        badge="3"
        title="校准完成，下一步预览调参"
        action="回工作台"
        @action="goStudio"
      >
        回到工作台调整参数，右侧会自动生成四联预览。
      </GuideCard>
    </section>

    <section class="card main">
      <div class="main-head">
        <h3 class="card-title">画布校准</h3>
        <div class="actions">
          <div class="seg">
            <button class="seg-btn" :class="{ active: calibTarget === 'canvas' }" @click="calibTarget = 'canvas'">画布</button>
            <button class="seg-btn" :class="{ active: calibTarget === 'undo' }" @click="calibTarget = 'undo'">撤销按钮</button>
            <button class="seg-btn" :class="{ active: calibTarget === 'fold' }" @click="calibTarget = 'fold'">开合按钮</button>
            <button class="seg-btn" :class="{ active: calibTarget === 'slider' }" @click="calibTarget = 'slider'">笔粗细滑杆</button>
          </div>
          <button class="btn btn-sm" :disabled="!projectStore.selectedHwnd || projectStore.busy" @click="refreshCapture">
            刷新截图
          </button>
          <button
            v-if="calibTarget === 'canvas'"
            class="btn btn-sm"
            :disabled="!projectStore.selectedHwnd || projectStore.busy"
            title="点击后自动把小程序带到前台再截图识别"
            @click="onAuto"
          >
            自动识别
          </button>
          <button
            v-if="!autoRunning"
            class="btn btn-sm"
            :disabled="!projectStore.project || projectStore.busy"
            title="自动识别画布并行为探测撤销/折叠钮/滑杆/橡皮/画笔（含收起态重扫），期间会自动点击画板工具条"
            @click="onRunAuto"
          >
            一键自动校准
          </button>
          <button v-else class="btn btn-sm btn-danger" @click="onStopAuto">停止校准</button>
          <span v-if="autoRunning" class="esc-tip">随时按 ESC / 空格急停</span>
          <button
            class="btn btn-primary btn-sm"
            :disabled="primaryDisabled"
            @click="calibTarget === 'canvas' ? onManual() : onSaveExtras()"
          >
            {{ primaryLabel }}
          </button>
        </div>
      </div>

      <p class="tip">
        框选前请先在画板底部点「画笔」进入绘画面板（初始仅有画笔/文字/贴纸）。
        「画布」用于落笔定位（必填）；「开合按钮」「笔粗细滑杆」「撤销按钮」仅在开启自动变粗时需要框选。
        开合按钮是同一位置、功能相反的两个独立按钮：收起态点「∨」展开、展开态点「∧」收起；
        「大小」滑杆只在展开态（点「∧/∨」后）出现，「撤销 ↩」浮在画布左下侧。
        程序绘制前自动扫描滑杆各档实测笔宽，标定测试线用撤销清空，绘制时自动折叠/展开下方工具面板。
      </p>

      <div v-if="autoRunning || autoNote || autoSteps.length" class="auto-panel">
        <div v-if="!autoRunning" class="auto-targets">
          <span class="targets-label">单独识别</span>
          <label v-for="choice in TARGET_CHOICES" :key="choice.id" class="target-check">
            <input type="checkbox" :checked="pickTargets.has(choice.id)" @change="toggleTarget(choice.id)" />
            {{ choice.label }}
          </label>
          <button v-if="pickTargets.size" class="btn btn-sm" @click="pickTargets = new Set()">
            清空（全量）
          </button>
        </div>
        <p v-if="autoNote" class="banner banner-info target-tip">{{ autoNote }}</p>
        <ul v-if="autoSteps.length" class="step-list">
          <li v-for="item in autoSteps" :key="item.step" :class="`step-${item.state}`">
            <span class="step-icon">{{ stepIcon(item.state) }}</span>{{ item.message }}
          </li>
        </ul>
      </div>
      <div class="diagram-row">
        <CalibDiagram :mode="calibTarget" class="diagram" />
        <div class="diagram-hint">
          <div class="banner banner-info target-tip">{{ editorHint }}</div>
          <p class="tip diagram-note">
            示意图中金色高亮即当前要框选的控件在小程序画板上的大致位置；实际以右侧截图为准。
          </p>
        </div>
      </div>

      <RectEditor
        v-if="baseSrc"
        v-model="editorRect"
        :image-width="imageSize.w"
        :image-height="imageSize.h"
        :annotations="editorAnnotations"
        :annotations-dim="editorAnnotationsDim"
      >
        <img :src="baseSrc" class="capture" alt="窗口截图" draggable="false" @load="onCaptureLoad" />
      </RectEditor>
      <div v-else class="placeholder">选择目标窗口后自动加载截图</div>
    </section>
  </div>
</template>

<style scoped>
.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  padding: 80px 0;
  color: var(--text-dim);
}

.layout {
  align-items: start;
}

.main-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 4px;
}

.actions {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}

.seg {
  display: inline-flex;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  overflow: hidden auto;
  max-width: 100%;
  scrollbar-width: none;
}

.seg-btn {
  border: none;
  background: transparent;
  color: var(--text-dim);
  font-size: 12px;
  padding: 5px 10px;
  cursor: pointer;
  flex: none;
  white-space: nowrap;
}

/* 触摸设备：切换条与说明文字提一档 */
@media (pointer: coarse), (max-width: 1376px) {
  .seg-btn,
  .tip,
  .target-tip {
    font-size: 13px;
  }
}

.seg-btn + .seg-btn {
  border-left: 1px solid var(--line);
}

.seg-btn.active {
  background: var(--gold-soft, rgba(216, 169, 78, 0.18));
  color: var(--gold);
}

.tip {
  color: var(--text-faint);
  font-size: 12px;
  margin: 0 0 12px;
}

.capture {
  display: block;
  width: 100%;
  height: auto;
}

.placeholder {
  height: 320px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-faint);
  border: 1px dashed var(--line);
  border-radius: var(--radius-sm);
}

/* ---------- 一键自动校准面板 ---------- */

.auto-panel {
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-sm);
  background: var(--ink-2);
  padding: 10px 12px;
  margin-bottom: 12px;
  display: grid;
  gap: 8px;
}

.auto-panel .target-tip {
  margin-bottom: 0;
}

.auto-targets {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  font-size: 12px;
}

.targets-label {
  color: var(--text-faint);
}

.target-check {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  color: var(--text-dim);
}

.step-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 3px;
  font-size: 12px;
  max-height: 150px;
  overflow-y: auto;
}

.step-list li {
  color: var(--text-dim);
}

.step-icon {
  display: inline-block;
  width: 1.4em;
  text-align: center;
  color: var(--text-faint);
}

.step-done .step-icon {
  color: #57c785;
}

.step-failed .step-icon {
  color: #e06c6c;
}

.step-running {
  color: var(--text);
}

.esc-tip {
  font-size: 12px;
  color: var(--text-faint);
}

.step-running .step-icon {
  color: var(--gold);
}

/* 示意图与说明并排；窄屏自动堆叠 */
.diagram-row {
  display: flex;
  gap: 14px;
  align-items: flex-start;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.diagram {
  flex: 0 1 300px;
  min-width: 220px;
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-sm);
  background: var(--ink-2);
  padding: 6px;
}

.diagram-hint {
  flex: 1 1 240px;
  min-width: 0;
}

.diagram-note {
  margin-bottom: 0;
}

.target-tip {
  font-size: 12px;
  margin-bottom: 10px;
}

.mirror-btn {
  display: block;
  width: 100%;
  margin-bottom: 6px;
}

.divider {
  height: 1px;
  background: var(--line-soft);
  margin: 16px 0;
}

.meta {
  margin: 0;
  display: grid;
  gap: 8px;
  font-size: 13px;
}

.meta > div {
  display: flex;
  justify-content: space-between;
}

.meta dt {
  color: var(--text-faint);
}

.meta dd {
  margin: 0;
  color: var(--text);
}
</style>
