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

// 默认选中目标窗口（自动或首次枚举命中）后自动跑一次识别；
// 用户手动换窗口或已有校准结果时不触发，避免覆盖手动校准。
const autoRan = shallowRef(false)
watch(
  () => projectStore.selectedHwnd,
  (hwnd) => {
    const target = projectStore.targetWindow
    if (autoRan.value || hwnd === null) return
    if (!target || hwnd !== target.hwnd) return
    autoRan.value = true
    if (!projectStore.calibrated) onAuto()
  },
  { immediate: true },
)

function refreshCapture() {
  captureNonce.value += 1
}

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
  const result = await projectStore.runCalibration('manual', rect)
  if (result) lastConfidence.value = result.confidence
}

function goStudio() {
  emit('goto', 'studio')
}

/** 目标窗口提示：未找到 / 已选中 / 用户选了别的窗口，三种口吻不同。 */
const targetTip = computed(() => {
  const target = projectStore.targetWindow
  if (!target) {
    return { tone: 'warn' as const, text: `未找到「${TARGET_WINDOW_KEYWORD}」小程序窗口。请先在电脑上打开小程序并进入画板页，再点「重新枚举」。` }
  }
  if (projectStore.selectedHwnd === target.hwnd) {
    return { tone: 'info' as const, text: `绘制目标为「${TARGET_WINDOW_KEYWORD}」小程序窗口（已自动选中），请保持其可见。` }
  }
  return { tone: 'warn' as const, text: `当前选中的不是「${TARGET_WINDOW_KEYWORD}」小程序窗口，绘制只会落笔到所选窗口，请确认。` }
})
</script>

<template>
  <div v-if="!projectStore.project" class="empty">
    <p>校准前需要先导入原图。</p>
    <button class="btn btn-primary" @click="goStudio">去工作台导入图片</button>
  </div>

  <div v-else class="layout">
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
      <h3 class="card-title">识别结果</h3>
      <dl class="meta">
        <div><dt>画布矩形</dt><dd class="num">{{ rectText }}</dd></div>
        <div><dt>截图尺寸</dt><dd class="num">{{ imageSize.w }}×{{ imageSize.h }}</dd></div>
        <div><dt>DPI 缩放</dt><dd class="num">{{ projectStore.calibration?.dpiScale ?? '—' }}</dd></div>
        <div><dt>置信度</dt><dd class="num">{{ lastConfidence ?? '—' }}</dd></div>
      </dl>

      <ErrorBanner v-if="projectStore.error" kind="error">{{ projectStore.error }}</ErrorBanner>
      <div v-if="notDetected" class="banner banner-warn">未识别到合格画布，请在右侧手动框选。</div>
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
          <button class="btn btn-sm" :disabled="!projectStore.selectedHwnd || projectStore.busy" @click="refreshCapture">
            刷新截图
          </button>
          <button class="btn btn-sm" :disabled="!projectStore.selectedHwnd || projectStore.busy" @click="onAuto">
            自动识别
          </button>
          <button
            class="btn btn-primary btn-sm"
            :disabled="!manualRect || manualRect.w < 4 || manualRect.h < 4 || projectStore.busy"
            @click="onManual"
          >
            应用手动框选
          </button>
        </div>
      </div>

      <p class="tip">
        自动识别适用于白底画布；若画布有内容或被遮挡，请按住拖动框出白色绘图区。框选坐标基于截图，落笔时由后端换算到屏幕。
      </p>

      <RectEditor
        v-if="captureSrc"
        v-model="manualRect"
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
  display: grid;
  grid-template-columns: 300px 1fr;
  gap: 16px;
  align-items: start;
}

.main-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 4px;
}

.actions {
  display: flex;
  gap: 8px;
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
