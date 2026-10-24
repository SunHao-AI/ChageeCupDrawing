<script setup lang="ts">
import { computed, onUnmounted, shallowRef, watch } from 'vue'
import type { AssetKind, PlanSummary } from '@/api/types'

const props = defineProps<{ summary: PlanSummary; pending: boolean }>()

const ASSETS: { kind: AssetKind; label: string }[] = [
  { kind: 'composite', label: '合成预览' },
  { kind: 'simulated', label: '模拟笔迹' },
  { kind: 'difference', label: '差异图' },
  { kind: 'target', label: '目标掩膜' },
]

const active = shallowRef<AssetKind>('composite')
const current = computed(() => ASSETS.find((a) => a.kind === active.value) ?? ASSETS[0])

// 规划要 10–30 秒，用秒表让「自动重新规划」可感知。
const elapsed = shallowRef(0)
let timer: ReturnType<typeof setInterval> | null = null

watch(
  () => props.pending,
  (busy) => {
    if (timer) clearInterval(timer)
    timer = null
    if (busy) {
      elapsed.value = 0
      timer = setInterval(() => (elapsed.value += 1), 1000)
    }
  },
)

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div class="viewer">
    <div class="thumbs">
      <button
        v-for="asset in ASSETS"
        :key="asset.kind"
        class="thumb"
        :class="{ active: active === asset.kind }"
        @click="active = asset.kind"
      >
        <img :src="summary.assets[asset.kind]" :alt="asset.label" loading="lazy" />
        <span class="thumb-label">{{ asset.label }}</span>
      </button>
    </div>

    <div class="frame">
      <img :key="current.kind + summary.planId" :src="summary.assets[current.kind]" :alt="current.label" />
      <div v-if="props.pending" class="overlay">
        <div class="spinner"></div>
        <div class="overlay-title num">重新规划中 {{ elapsed }}s</div>
        <div class="overlay-sub">参数调整后自动重新规划，完成后此处与缩略图同步更新</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.viewer {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.thumbs {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
}

@media (max-width: 1023px) {
  .thumbs {
    grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
  }
}

.thumb {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px;
  background: var(--ink-1);
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: border-color 0.15s;
}

.thumb:hover {
  border-color: var(--line);
}

.thumb.active {
  border-color: var(--gold);
  background: var(--gold-dim);
}

.thumb img {
  width: 100%;
  height: 96px;
  object-fit: contain;
  border-radius: 4px;
  background: #fff;
}

.thumb-label {
  font-size: 11px;
  color: var(--text-dim);
  text-align: center;
}

@media (pointer: coarse), (max-width: 1376px) {
  .thumb-label,
  .overlay-sub {
    font-size: 12px;
  }
}

.thumb.active .thumb-label {
  color: var(--gold);
}

.frame {
  position: relative;
  background:
    linear-gradient(45deg, var(--ink-1) 25%, transparent 25%, transparent 75%, var(--ink-1) 75%),
    linear-gradient(45deg, var(--ink-1) 25%, var(--ink-2) 25%, var(--ink-2) 75%, var(--ink-1) 75%);
  background-size: 24px 24px;
  background-position: 0 0, 12px 12px;
  border: 1px solid var(--line-soft);
  border-radius: var(--radius);
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 260px;
  max-height: 54vh;
  overflow: hidden;
  padding: 14px;
}

.frame img {
  max-width: 100%;
  max-height: calc(54vh - 28px);
  width: auto;
  object-fit: contain;
  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.45);
}

.overlay {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  background: rgba(15, 18, 16, 0.72);
  backdrop-filter: blur(2px);
}

.spinner {
  width: 34px;
  height: 34px;
  border-radius: 50%;
  border: 3px solid var(--line);
  border-top-color: var(--gold);
  animation: spin 0.9s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.overlay-title {
  font-size: 14px;
  color: var(--gold);
  font-weight: 600;
}

.overlay-sub {
  font-size: 12px;
  color: var(--text-dim);
}
</style>
