<script setup lang="ts">
import { computed, onMounted, onUnmounted, shallowRef, useTemplateRef } from 'vue'
import type { CalibAnnotation, Rect } from '@/api/types'

const props = defineProps<{
  /** 截图原始尺寸（图像坐标系）。 */
  imageWidth: number
  imageHeight: number
  modelValue: Rect | null
  /** 只读标注层（探测/校准结果）；随图缩放，不参与框选交互。 */
  annotations?: CalibAnnotation[]
  /** 帧不符（标注绑定的快照与当前底图不是同一帧）时淡出而非硬切。 */
  annotationsDim?: boolean
}>()

const emit = defineEmits<{ 'update:modelValue': [rect: Rect | null] }>()

const stageRef = useTemplateRef<HTMLDivElement>('stage')

// 显示像素 → 图像坐标 的缩放，按当前显示宽度实时计算。
function scale(): number {
  const el = stageRef.value
  if (!el || props.imageWidth <= 0) return 1
  return props.imageWidth / el.clientWidth
}

// 标注层要随窗口缩放重算，clientWidth 不是响应式数据，用 ResizeObserver 同步。
const displayWidth = shallowRef(0)
let observer: ResizeObserver | null = null

const factor = computed(() =>
  displayWidth.value > 0 && props.imageWidth > 0 ? displayWidth.value / props.imageWidth : 1,
)

const rectStyle = computed(() => {
  const rect = props.modelValue
  if (!rect) return null
  return {
    left: `${rect.x * factor.value}px`,
    top: `${rect.y * factor.value}px`,
    width: `${rect.w * factor.value}px`,
    height: `${rect.h * factor.value}px`,
  }
})

const ANNOTATION_COLORS: Record<string, string> = {
  canvas: 'var(--gold, #d8a94e)',
  slider: '#4da3ff',
  undo: '#57c785',
  fold: '#b58cff',
  eraser: '#ff9f57',
  brush: '#ff6b9d',
}

const annotationStyles = computed(() => {
  const k = factor.value
  return (props.annotations ?? []).map((item) => {
    const color = ANNOTATION_COLORS[item.step] ?? '#9ad0ff'
    const base: Record<string, string> = { '--anno-color': color }
    if (item.kind === 'rect') {
      return {
        item,
        style: {
          ...base,
          left: `${item.x * k}px`,
          top: `${item.y * k}px`,
          width: `${(item.w ?? 0) * k}px`,
          height: `${(item.h ?? 0) * k}px`,
        },
      }
    }
    const size = Math.max(14, 10 * k)
    return {
      item,
      style: {
        ...base,
        left: `${item.x * k - size / 2}px`,
        top: `${item.y * k - size / 2}px`,
        width: `${size}px`,
        height: `${size}px`,
      },
    }
  })
})

let originX = 0
let originY = 0
let dragging = false

function toImage(event: PointerEvent): { x: number; y: number } {
  const el = stageRef.value!
  const box = el.getBoundingClientRect()
  const k = scale()
  return {
    // 截图右/下边缘外一点点也会 round 到 imageWidth，必须钳到 [0, size-1]，
    // 否则后端按 rect.x + rect.w > 截图宽 判为越界（400 E_INVALID_ARGUMENT）。
    x: clamp(Math.round((event.clientX - box.left) * k), props.imageWidth - 1),
    y: clamp(Math.round((event.clientY - box.top) * k), props.imageHeight - 1),
  }
}

function clamp(value: number, max: number): number {
  return Math.max(0, Math.min(max, value))
}

function onPointerDown(event: PointerEvent) {
  dragging = true
  const p = toImage(event)
  originX = p.x
  originY = p.y
  emit('update:modelValue', { x: p.x, y: p.y, w: 1, h: 1 })
  ;(event.target as Element).setPointerCapture?.(event.pointerId)
}

function onPointerMove(event: PointerEvent) {
  if (!dragging) return
  const p = toImage(event)
  const x = clamp(Math.min(originX, p.x), props.imageWidth - 1)
  const y = clamp(Math.min(originY, p.y), props.imageHeight - 1)
  // 尺寸至少 1 px，且 x+w 不得超过图像宽度（后端以 x+w ≤ width 校验）。
  const w = clamp(Math.abs(p.x - originX), props.imageWidth - x - 1)
  const h = clamp(Math.abs(p.y - originY), props.imageHeight - y - 1)
  emit('update:modelValue', { x, y, w: Math.max(1, w), h: Math.max(1, h) })
}

function onPointerUp() {
  dragging = false
}

onMounted(() => {
  window.addEventListener('pointerup', onPointerUp)
  const el = stageRef.value
  if (el) {
    displayWidth.value = el.clientWidth
    observer = new ResizeObserver(() => {
      displayWidth.value = el.clientWidth
    })
    observer.observe(el)
  }
})
onUnmounted(() => {
  window.removeEventListener('pointerup', onPointerUp)
  observer?.disconnect()
  observer = null
})
</script>

<template>
  <div ref="stage" class="stage" @pointerdown="onPointerDown" @pointermove="onPointerMove">
    <slot />
    <div
      v-for="entry in annotationStyles"
      :key="entry.item.step + (entry.item.dashed ? '-d' : '')"
      class="annotation"
      :class="[entry.item.kind === 'point' ? 'anno-point' : 'anno-rect', { dashed: entry.item.dashed }]"
      :style="entry.style"
    >
      <span class="anno-label">{{ entry.item.label }}</span>
    </div>
    <div v-if="annotations && annotations.length && annotationsDim" class="anno-fade" aria-hidden="true"></div>
    <div v-if="rectStyle" class="rect" :style="rectStyle"></div>
    <div v-else-if="!annotations || !annotations.length" class="hint">
      在截图上按住拖动，框出白色杯贴画布区域
    </div>
  </div>
</template>

<style scoped>
.stage {
  position: relative;
  width: 100%;
  user-select: none;
  cursor: crosshair;
  /* 手指在画面上拖框时禁止浏览器平移/缩放页面 */
  touch-action: none;
  background: var(--ink-0);
  border-radius: var(--radius-sm);
  overflow: hidden;
}

.rect {
  position: absolute;
  border: 2px solid var(--gold);
  background: rgba(216, 169, 78, 0.12);
  pointer-events: none;
  box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.4);
}

.hint {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-faint);
  font-size: 13px;
  pointer-events: none;
}

/* ---------- 只读标注层 ---------- */

.annotation {
  position: absolute;
  z-index: 2;
  pointer-events: none;
  transition: opacity 0.3s ease;
}

.anno-rect {
  border: 2px solid var(--anno-color);
  border-radius: 3px;
}

.anno-rect.dashed {
  border-style: dashed;
  border-width: 1.5px;
  opacity: 0.85;
}

.anno-point {
  border: 2px solid var(--anno-color);
  border-radius: 999px;
  background: color-mix(in srgb, var(--anno-color) 25%, transparent);
  box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.45);
}

.anno-label {
  position: absolute;
  left: 50%;
  bottom: calc(100% + 3px);
  transform: translateX(-50%);
  white-space: nowrap;
  font-size: 11px;
  line-height: 1.4;
  padding: 0 5px;
  border-radius: 3px;
  color: var(--anno-color);
  background: rgba(0, 0, 0, 0.62);
}

/* 帧不符：整层标注盖半透明遮罩淡出，底图保持可读 */
.anno-fade {
  position: absolute;
  inset: 0;
  z-index: 3;
  pointer-events: none;
  background: rgba(12, 12, 14, 0.45);
}
</style>
