<script setup lang="ts">
import { computed } from 'vue'
import { usePlanStore } from '@/stores/plan'
import { useProjectStore } from '@/stores/project'
import { useTaskStore } from '@/stores/task'
import type { ViewKey } from '@/views/types'

const emit = defineEmits<{ goto: [view: ViewKey] }>()

const projectStore = useProjectStore()
const planStore = usePlanStore()
const taskStore = useTaskStore()

interface Step {
  label: string
  hint: string
  view: ViewKey
  done: boolean
}

// 全部状态从 store 派生：换图后 ②③④ 自动回到未完成。
const steps = computed<Step[]>(() => [
  { label: '导入图片', hint: '上传 PNG / JPEG 线稿', view: 'studio', done: projectStore.project !== null },
  { label: '校准画布', hint: '选择目标窗口并识别白色画布', view: 'calibrate', done: projectStore.calibrated },
  { label: '预览调参', hint: '调整参数，查看四联预览', view: 'studio', done: planStore.summary !== null && !planStore.pending },
  { label: '开始绘制', hint: '倒计时后自动落笔', view: 'run', done: taskStore.status === 'DONE' },
])

// 当前步 = 第一个未完成步；全部完成时为 -1（只显示 ✓）。
const currentIndex = computed(() => steps.value.findIndex((step) => !step.done))
</script>

<template>
  <nav class="stepper" aria-label="操作流程">
    <template v-for="(step, index) in steps" :key="step.label">
      <span v-if="index > 0" class="sep"></span>
      <button
        class="step"
        :class="{ done: step.done, current: index === currentIndex, future: !step.done && index > currentIndex }"
        :title="step.hint"
        @click="emit('goto', step.view)"
      >
        <span class="dot num">{{ step.done ? '✓' : index + 1 }}</span>
        <span class="label">{{ step.label }}</span>
      </button>
    </template>
  </nav>
</template>

<style scoped>
.stepper {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 7px 20px;
  background: var(--ink-1);
  border-bottom: 1px solid var(--line-soft);
  flex: none;
}

.sep {
  width: 22px;
  height: 1px;
  background: var(--line);
  margin: 0 4px;
  flex: none;
}

.step {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  border: none;
  background: transparent;
  color: var(--text-faint);
  font-size: 12.5px;
  padding: 3px 9px;
  border-radius: 999px;
  cursor: pointer;
  transition: color 0.15s;
}

.step:hover {
  color: var(--text);
}

.dot {
  flex: none;
  width: 19px;
  height: 19px;
  border-radius: 50%;
  border: 1px solid var(--line);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  transition: background 0.15s, border-color 0.15s, color 0.15s;
}

.step.done {
  color: var(--text-dim);
}

.step.done .dot {
  border-color: var(--green);
  color: var(--green);
}

.step.current {
  color: var(--gold);
  background: var(--gold-dim);
}

.step.current .dot {
  background: var(--gold);
  border-color: var(--gold);
  color: #1a1408;
  font-weight: 700;
}

/* 窄屏：步骤条横向滑动而不是把文字挤成竖排 */
@media (max-width: 1023px) {
  .stepper {
    overflow-x: auto;
    scrollbar-width: none;
    padding: 7px 12px;
  }

  .step {
    flex: none;
    white-space: nowrap;
  }
}

/* 平板/触摸端：序号圆点数字提一档 */
@media (pointer: coarse), (max-width: 1376px) {
  .dot {
    font-size: 12px;
  }
}
</style>
