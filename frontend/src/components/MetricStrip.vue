<script setup lang="ts">
import { computed } from 'vue'
import type { PlanMetrics } from '@/api/types'

const props = defineProps<{ metrics: PlanMetrics; workScale?: number }>()

interface MetricCell {
  key: string
  label: string
  value: string
  low: boolean
}

const LOW = 0.9

const cells = computed<MetricCell[]>(() => {
  const m = props.metrics
  return [
    { key: 'precision', label: '精确率', value: m.precision.toFixed(3), low: m.precision < LOW },
    { key: 'recall', label: '召回率', value: m.recall.toFixed(3), low: m.recall < LOW },
    { key: 'iou', label: 'IoU', value: m.iou.toFixed(3), low: m.iou < LOW },
    { key: 'pathCount', label: '笔迹数', value: m.pathCount.toLocaleString(), low: false },
    {
      key: 'totalLength',
      label: '总长',
      value: `${Math.round(m.totalLength).toLocaleString()} px`,
      low: false,
    },
    ...(props.workScale !== undefined
      ? [{ key: 'workScale', label: '工作倍率', value: `×${props.workScale}`, low: false }]
      : []),
  ]
})
</script>

<template>
  <div class="metrics">
    <div v-for="cell in cells" :key="cell.key" class="cell">
      <div class="label">{{ cell.label }}</div>
      <div class="value num" :class="{ low: cell.low }">{{ cell.value }}</div>
    </div>
  </div>
</template>

<style scoped>
.metrics {
  display: grid;
  grid-template-columns: repeat(v-bind('cells.length'), 1fr);
  gap: 1px;
  background: var(--line-soft);
  border: 1px solid var(--line-soft);
  border-radius: var(--radius);
  overflow: hidden;
}

.cell {
  background: var(--ink-1);
  padding: 10px 12px;
}

.label {
  font-size: 11px;
  color: var(--text-faint);
  margin-bottom: 4px;
}

.value {
  font-size: 15px;
  color: var(--text);
}

.value.low {
  color: var(--amber);
}
</style>
