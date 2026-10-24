<script setup lang="ts">
import { computed, onMounted, onUnmounted, useTemplateRef } from 'vue'
import type { Rect } from '@/api/types'

const props = defineProps<{
  /** 截图原始尺寸（图像坐标系）。 */
  imageWidth: number
  imageHeight: number
  modelValue: Rect | null
}>()

const emit = defineEmits<{ 'update:modelValue': [rect: Rect | null] }>()

const stageRef = useTemplateRef<HTMLDivElement>('stage')

// 显示像素 → 图像坐标 的缩放，按当前显示宽度实时计算。
function scale(): number {
  const el = stageRef.value
  if (!el || props.imageWidth <= 0) return 1
  return props.imageWidth / el.clientWidth
}

const rectStyle = computed(() => {
  const rect = props.modelValue
  if (!rect) return null
  const el = stageRef.value
  const factor = el && props.imageWidth > 0 ? el.clientWidth / props.imageWidth : 1
  return {
    left: `${rect.x * factor}px`,
    top: `${rect.y * factor}px`,
    width: `${rect.w * factor}px`,
    height: `${rect.h * factor}px`,
  }
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

onMounted(() => window.addEventListener('pointerup', onPointerUp))
onUnmounted(() => window.removeEventListener('pointerup', onPointerUp))
</script>

<template>
  <div ref="stage" class="stage" @pointerdown="onPointerDown" @pointermove="onPointerMove">
    <div v-if="rectStyle" class="rect" :style="rectStyle"></div>
    <div v-else class="hint">在截图上按住拖动，框出白色杯贴画布区域</div>
    <slot />
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
</style>
