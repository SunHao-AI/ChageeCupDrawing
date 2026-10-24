<script setup lang="ts">
import { computed, shallowRef, watch } from 'vue'
import { windowCaptureUrl } from '@/api/endpoints'
import ErrorBanner from '@/components/ErrorBanner.vue'
import GuideCard from '@/components/GuideCard.vue'
import RectEditor from '@/components/RectEditor.vue'
import { TARGET_WINDOW_KEYWORD, useProjectStore } from '@/stores/project'
import type { Rect } from '@/api/types'
import type { ViewEmits } from './types'

const emit = defineEmits<ViewEmits>()
const projectStore = useProjectStore()

const captureNonce = shallowRef(0)
const manualRect = shallowRef<Rect | null>(null)
const lastConfidence = shallowRef<number | null>(null)
const notDetected = shallowRef(false)
// 首次校准前 calibration 为空，用截图自身加载出的自然尺寸做框选坐标基准。
const naturalSize = shallowRef({ w: 0, h: 0 })

// 框选目标四模式：画布 / 笔粗细滑杆轨道 / 撤销按钮 / 面板折叠按钮（自动变粗标定用）。
type CalibTarget = 'canvas' | 'slider' | 'undo' | 'fold'
const calibTarget = shallowRef<CalibTarget>('canvas')
const sliderRect = shallowRef<Rect | null>(null)
const undoRect = shallowRef<Rect | null>(null)
const foldRect = shallowRef<Rect | null>(null)

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
      ? '框住笔「粗细」滑杆的整条轨道（不含滑块圆点大小，尽量贴轨道两端）'
      : calibTarget.value === 'undo'
        ? '点一下或框住「撤销」按钮的中心位置（标定测试线自动清空用）'
        : '点一下或框住画板下方的「∨」折叠按钮中心（绘制时自动折叠/展开工具面板用）',
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
}

// 目标在 PC 窗口与投屏窗口之间切换时，旧校准的坐标系已失效，自动重新识别一次。
watch(
  () => projectStore.deviceKind,
  () => {
    autoRan.value = false
    if (projectStore.selectedHwnd !== null) {
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
            <button class="seg-btn" :class="{ active: calibTarget === 'slider' }" @click="calibTarget = 'slider'">笔粗细滑杆</button>
            <button class="seg-btn" :class="{ active: calibTarget === 'undo' }" @click="calibTarget = 'undo'">撤销按钮</button>
            <button class="seg-btn" :class="{ active: calibTarget === 'fold' }" @click="calibTarget = 'fold'">折叠按钮</button>
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
            class="btn btn-primary btn-sm"
            :disabled="primaryDisabled"
            @click="calibTarget === 'canvas' ? onManual() : onSaveExtras()"
          >
            {{ primaryLabel }}
          </button>
        </div>
      </div>

      <p class="tip">
        「画布」用于落笔定位（必填）；「笔粗细滑杆」「撤销按钮」「折叠按钮」仅在开启自动变粗时需要框选：程序绘制前自动扫描滑杆各档实测笔宽，标定测试线用撤销清空，绘制时自动折叠/展开下方工具面板。
      </p>
      <div v-if="calibTarget !== 'canvas'" class="banner banner-info target-tip">{{ editorHint }}</div>

      <RectEditor
        v-if="captureSrc"
        v-model="editorRect"
        :image-width="imageSize.w"
        :image-height="imageSize.h"
      >
        <img :src="captureSrc" class="capture" alt="窗口截图" draggable="false" @load="onCaptureLoad" />
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
