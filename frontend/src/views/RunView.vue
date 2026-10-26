<script setup lang="ts">
import { computed, onMounted, onUnmounted, shallowRef } from 'vue'
import ErrorBanner from '@/components/ErrorBanner.vue'
import GuideCard from '@/components/GuideCard.vue'
import ParamField from '@/components/ParamField.vue'
import { EXEC_FIELDS, TASK_CONTROLS } from '@/params/registry'
import { usePlanStore } from '@/stores/plan'
import { useParamsStore } from '@/stores/params'
import { useProjectStore } from '@/stores/project'
import { useTaskStore } from '@/stores/task'
import type { ViewEmits, ViewKey } from './types'

const emit = defineEmits<ViewEmits>()
const taskStore = useTaskStore()
const planStore = usePlanStore()
const paramsStore = useParamsStore()
const projectStore = useProjectStore()

const countdownMs = shallowRef<number>(TASK_CONTROLS.countdownMs.default)
const allowDrift = shallowRef<number>(TASK_CONTROLS.allowDrift.default)
// 窄屏下参数侧栏折叠为抽屉；桌面端 CSS 保证侧栏常开，此状态无效果。
const paramsOpen = shallowRef(false)

const planReady = computed(() => planStore.summary !== null && !planStore.pending)
const running = computed(() => taskStore.isActive)
const progress = computed(() => taskStore.progress)
const snapshot = computed(() => taskStore.snapshot)

/** 守护/急停触发的暂停：语义是「等用户切回目标后手动续画」，区别于普通停止。 */
const GUARD_STOP_HINTS: Record<string, string> = {
  focus_lost: '目标窗口失去焦点或已最小化，已立即抬笔。请切回小程序窗口，再从断点续画。',
  mouse_takeover: '检测到鼠标被你接管，已立即抬笔。回到小程序窗口后从断点续画即可。',
  esc: '已按 ESC 急停，笔已抬起。可从断点续画。',
}
const guardStopHint = computed(() =>
  snapshot.value?.status === 'STOPPED' && snapshot.value.stopReason
    ? GUARD_STOP_HINTS[snapshot.value.stopReason] ?? null
    : null,
)

const statusLabel = computed(() => {
  switch (snapshot.value?.status) {
    case 'ARMING':
      return '待命中'
    case 'DRAWING':
      return '绘制中'
    case 'STOPPING':
      return '正在停止…'
    case 'DONE':
      return '绘制完成'
    case 'STOPPED':
      return guardStopHint.value ? '已暂停，可续画' : '已停止，可续画'
    case 'FAILED':
      return `失败：${snapshot.value?.errorCode ?? ''}`
    default:
      return '空闲'
  }
})

const elapsed = computed(() => {
  const ms = progress.value?.elapsedMs ?? 0
  return `${Math.floor(ms / 1000)} 秒`
})

const showStartGuide = computed(
  () => taskStore.canStart && planReady.value && projectStore.calibrated,
)

// 失败错误码 → 下一步操作建议（design 4.4 错误码表的操作化）。
const ADVICE: Record<string, { text: string; goto?: ViewKey; action?: string }> = {
  E_FOREGROUND_MISMATCH: { text: '把目标窗口切到前台（点一下它的画面），再点「开始绘制」' },
  E_CANVAS_DRIFT: { text: '画布被移动或缩放，需要重新识别', goto: 'calibrate', action: '去校准' },
  E_WINDOW_LOST: { text: '目标窗口已关闭，重开画板后重新校准', goto: 'calibrate', action: '去校准' },
  E_CANVAS_MISSING: { text: '还没有校准画布', goto: 'calibrate', action: '去校准' },
  E_CAPTURE_FAILED: { text: '窗口截图失败：确认窗口未最小化，再点「开始绘制」重试' },
  E_EXECUTOR_UNAVAILABLE: { text: '鼠标注入不可用：确认在 Windows 上运行且系统未拦截 SendInput' },
}

const advice = computed(() => {
  if (snapshot.value?.status !== 'FAILED') return null
  const code = snapshot.value.errorCode
  return code ? (ADVICE[code] ?? null) : null
})

async function onStart() {
  if (!planStore.summary) return
  await taskStore.start(planStore.summary.planId, countdownMs.value, allowDrift.value)
}

async function onResume() {
  await taskStore.resume(countdownMs.value, allowDrift.value)
}

/** 窄屏抽屉交互：参数调整完（滑杆松手/开关切换触发 change）自动收起。 */
function onParamChanged() {
  if (window.innerWidth < 1024) paramsOpen.value = false
}

// 手机浏览器会冻结后台标签的 SSE；回到前台时主动拉一次快照、重接事件流。
function onVisibility() {
  if (document.visibilityState === 'visible') taskStore.ensureStream()
}

onMounted(() => {
  taskStore.ensureStream()
  document.addEventListener('visibilitychange', onVisibility)
})

onUnmounted(() => {
  document.removeEventListener('visibilitychange', onVisibility)
  taskStore.detach()
})
</script>

<template>
  <div class="split run" style="--split-side: 340px">
    <button class="btn btn-sm params-toggle" @click="paramsOpen = !paramsOpen">
      {{ paramsOpen ? '收起执行参数' : '展开执行参数' }}
    </button>
    <aside class="card side" :class="{ open: paramsOpen }">
      <h3 class="card-title">执行参数</h3>
      <ParamField
        v-for="meta in EXEC_FIELDS"
        :key="meta.key"
        :meta="meta"
        :model-value="(paramsStore.exec as any)[meta.key]"
        :disabled="running"
        @update:model-value="paramsStore.setExec({ [meta.key]: $event } as any)"
        @change="onParamChanged"
      />
      <div
        v-if="paramsStore.exec.autoWidth && !projectStore.calibration?.slider && !projectStore.calibration?.toolsOpen"
        class="banner banner-warn auto-width-tip"
      >
        程序会先自动探测工具栏控件；个别控件识别失败时，再到「校准」页手动补标粗细滑杆轨道（建议同时标记撤销按钮与「∨」折叠按钮，分别用于标定后自动清空画布、绘制时自动折叠工具面板）。
      </div>

      <div class="divider"></div>
      <h3 class="card-title">任务控制</h3>
      <div class="field">
        <div class="field-label"><span>倒计时</span><span class="field-value num">{{ countdownMs }} ms</span></div>
        <input type="range" :min="TASK_CONTROLS.countdownMs.min" :max="TASK_CONTROLS.countdownMs.max" :step="TASK_CONTROLS.countdownMs.step" v-model.number="countdownMs" :disabled="running" />
        <div class="hint">倒计时内请切换到目标窗口，ESC 随时急停</div>
      </div>
      <div class="field">
        <div class="field-label"><span>允许漂移</span><span class="field-value num">{{ allowDrift.toFixed(2) }}</span></div>
        <input type="range" :min="TASK_CONTROLS.allowDrift.min" :max="TASK_CONTROLS.allowDrift.max" :step="TASK_CONTROLS.allowDrift.step" v-model.number="allowDrift" :disabled="running" />
        <div class="hint">落笔前画布位置变化超过该比例则拒绝落笔</div>
      </div>
    </aside>

    <section class="card stage">
      <div class="stage-head">
        <h3 class="card-title">绘制任务</h3>
        <span class="badge" :class="taskStore.isActive ? 'badge-gold' : ''">{{ statusLabel }}</span>
      </div>

      <div class="target num">
        <template v-if="projectStore.selectedWindow">目标：{{ projectStore.selectedWindow.title }}</template>
        <template v-else>目标窗口将在落笔前按校准记录定位</template>
      </div>

      <GuideCard
        v-if="snapshot?.status === 'ARMING'"
        tone="urgent"
        badge="!"
        title="倒计时中：立刻切换到目标窗口"
      >
        把画板窗口点到前台并保持画布可见。倒计时末段若目标不在前台会暂停等待，切回后自动继续。
      </GuideCard>
      <GuideCard
        v-else-if="showStartGuide"
        badge="4"
        title="点击「开始绘制」，自动切到目标窗口"
        action="开始绘制"
        @action="onStart"
      >
        ① 点开始 → ② 自动把小程序带到前台并倒计时（若被系统拦下再手动切一下）→ ③ 自动校验后自动落笔；按 ESC 可随时急停。
      </GuideCard>

      <div class="progress-block">
        <div class="pct num">{{ taskStore.percent }}<small>%</small></div>
        <div class="bar">
          <div class="bar-fill" :style="{ width: `${taskStore.percent}%` }"></div>
        </div>
        <div class="counts num">
          第 {{ snapshot?.cursorPath ?? 0 }} / {{ snapshot?.totalPaths ?? planStore.summary?.metrics.pathCount ?? 0 }} 笔
          <span v-if="progress"> · 已用 {{ elapsed }}</span>
        </div>
      </div>

      <ErrorBanner v-if="taskStore.error" kind="error">{{ taskStore.error }}</ErrorBanner>
      <ErrorBanner v-for="(warn, i) in taskStore.warnings" :key="i" kind="warn">{{ warn.message }}</ErrorBanner>
      <GuideCard
        v-if="guardStopHint"
        tone="urgent"
        badge="!"
        title="绘制已暂停"
        action="从断点续画"
        @action="onResume"
      >
        {{ guardStopHint }}
      </GuideCard>
      <div v-if="advice" class="advice">
        <span>{{ advice.text }}</span>
        <button v-if="advice.goto" class="btn btn-sm" @click="emit('goto', advice.goto)">{{ advice.action }}</button>
      </div>

      <div class="controls">
        <button
          class="btn btn-primary"
          :disabled="!taskStore.canStart || !planReady || !projectStore.calibrated || taskStore.busy"
          @click="onStart"
        >
          开始绘制
        </button>
        <button class="btn btn-danger" :disabled="!taskStore.canStop" @click="taskStore.stop()">停止</button>
        <button class="btn" :disabled="!taskStore.canResume || taskStore.busy" @click="onResume">从断点续画</button>
      </div>

      <p v-if="!projectStore.calibrated" class="banner banner-info">尚未校准画布，先去校准。</p>
      <p v-else-if="!planReady" class="banner banner-info">
        没有可绘制的规划结果，请回工作台生成规划。
        <button class="btn btn-sm" @click="$emit('goto', 'studio')">回工作台</button>
      </p>
    </section>
  </div>
</template>

<style scoped>
/* 分栏骨架复用全局 .split（<1024px 自动单列），此处只管抽屉与手机细节 */
.params-toggle {
  display: none;
}

/* 平板竖屏：参数栏折叠为顶部抽屉 */
@media (max-width: 1023px) {
  .params-toggle {
    display: flex;
    justify-self: start;
  }

  .side {
    display: none;
  }

  .side.open {
    display: block;
  }
}

/* 手机：进度大字号压缩、操作条全宽易点 */
@media (max-width: 639px) {
  .pct {
    font-size: 40px;
  }

  .controls {
    flex-wrap: wrap;
  }

  .controls .btn {
    flex: 1 1 auto;
    min-height: 44px;
  }
}

.side {
  max-height: calc(100vh - 108px);
  overflow-y: auto;
}

.hint {
  font-size: 11px;
  color: var(--text-faint);
  margin-top: 3px;
}

/* 触摸设备：小字号提一档 */
@media (pointer: coarse), (max-width: 1376px) {
  .hint {
    font-size: 12px;
  }
}

.divider {
  height: 1px;
  background: var(--line-soft);
  margin: 16px 0;
}

.stage {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-height: 480px;
}

.stage-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.stage-head .card-title {
  margin: 0;
}

.target {
  color: var(--text-dim);
  font-size: 13px;
}

.progress-block {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.pct {
  font-size: 56px;
  font-weight: 700;
  line-height: 1;
  color: var(--gold);
  letter-spacing: -0.02em;
}

.pct small {
  font-size: 20px;
  color: var(--gold-soft);
  margin-left: 4px;
}

.bar {
  height: 10px;
  background: var(--ink-3);
  border-radius: 999px;
  overflow: hidden;
}

.bar-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--gold-soft), var(--gold));
  border-radius: 999px;
  transition: width 0.25s ease;
}

.counts {
  color: var(--text-dim);
  font-size: 13px;
}

.controls {
  display: flex;
  gap: 10px;
  margin-top: auto;
}

.advice {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 9px 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--ink-2);
  font-size: 12.5px;
  color: var(--text-dim);
}

.controls .btn-danger:not(:disabled) {
  background: rgba(217, 108, 95, 0.14);
}
</style>
