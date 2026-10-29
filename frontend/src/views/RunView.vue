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
  mouse_takeover: '检测到鼠标被你接管，已立即抬笔。若误触在画布上留下了墨迹，可逐笔回退后再续画。',
  esc: '已按 ESC 急停，笔已抬起。可从断点续画。',
  pause: '已按空格暂停，笔已抬起。可从断点续画。',
  canvas_touched:
    '检测到画布上出现预期外墨迹（可能误触）。请对照投屏画面逐笔回退，清干净后从断点续画。',
  screen_changed: '画布整体变化（弹窗覆盖或被清空），请检查目标画面后再续画。',
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

// 「清空画布，从头绘制」组合动作（clear-and-guide-design §4.5）：
// confirm=旧墨全部丢弃的确认；confirmDirty=无撤销坐标时授权脏画布工具条探测。
const clearAsk = shallowRef<'none' | 'confirm' | 'confirmDirty'>('none')
const canClear = computed(
  () => projectStore.calibrated && taskStore.canStart && !taskStore.busy && !taskStore.clearing,
)

async function doClear(confirmDirtyProbe: boolean) {
  const projectId = projectStore.project?.projectId
  if (!projectId) return
  clearAsk.value = 'none'
  const result = await taskStore.clearCanvas(projectId, confirmDirtyProbe)
  if (result.code === 'E_CLEAR_UNAVAILABLE' && !confirmDirtyProbe) {
    clearAsk.value = 'confirmDirty'
    return
  }
  // 清空成功且画布确实干净了才接着从头开画；already_blank 也算成功。
  if (result.ok && planStore.summary) {
    await taskStore.start(planStore.summary.planId, countdownMs.value, allowDrift.value)
  }
}

// 引导卡分流（clear-and-guide-design §7.2）：按 warning 的 code 升级成带按钮的引导卡，
// 其余 warning 保持纯文字 banner。
const dirtySkipWarning = computed(
  () => taskStore.warnings.find((w) => w.code === 'probe_skipped_dirty') ?? null,
)
const probeFailWarning = computed(
  () => taskStore.warnings.find((w) => w.code === 'probe_failed') ?? null,
)
const plainWarnings = computed(
  () =>
    taskStore.warnings.filter(
      (w) => w.code !== 'probe_skipped_dirty' && w.code !== 'probe_failed',
    ),
)

/** 去校准页并预选要框选的控件（pendingCalibMark 由校准页 onMounted 一次性消费）。 */
function gotoCalibrateWith(mark: 'slider' | 'undo' | 'fold') {
  projectStore.requestCalibMark(mark)
  emit('goto', 'calibrate')
}

// 「删除任务」：只删任务进度记录，回到未开始绘制的初始态（规划/校准保留，
// 画板上的墨迹不会被清掉，需要时配合「清空画布，从头绘制」）。
const deleteAsk = shallowRef(false)

async function onDeleteTask() {
  deleteAsk.value = false
  await taskStore.remove()
}

/** 绘制中的暂停手势说明（注入模型下移动鼠标不可靠，别把它当暂停手段）。 */
const pauseGestureHint = computed(
  () =>
    running.value || snapshot.value?.status === 'ARMING'
      ? '随时暂停：按 ESC 或空格立即抬笔；在画布外按住鼠标左键也会判定接管自动暂停。刷新网页不会中断绘制（绘制在后台执行），请用「停止」或急停键。'
      : '',
)

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
        程序会先自动探测工具栏控件；个别控件识别失败时，先到画板底部点「画笔」进入绘画面板，再在「校准」页手动补标「大小」滑杆轨道（建议同时标记撤销「↩」与工具行最左的开合钮「∨/∧」，分别用于标定后自动清空画布、绘制时自动折叠/展开工具面板）。
      </div>

      <div class="divider"></div>
      <h3 class="card-title">任务控制</h3>
      <div class="field">
        <div class="field-label"><span>倒计时</span><span class="field-value num">{{ countdownMs }} ms</span></div>
        <input type="range" :min="TASK_CONTROLS.countdownMs.min" :max="TASK_CONTROLS.countdownMs.max" :step="TASK_CONTROLS.countdownMs.step" v-model.number="countdownMs" :disabled="running" />
        <div class="hint">倒计时内请切换到目标窗口，ESC / 空格随时急停</div>
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
        :title="
          taskStore.countdown !== null && taskStore.countdown > 0
            ? `倒计时 ${taskStore.countdown} 秒：立刻切换到目标窗口`
            : '即将落笔：保持目标窗口在前台'
        "
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
        ① 点开始 → ② 自动把小程序带到前台并倒计时（若被系统拦下再手动切一下）→ ③ 自动校验后自动落笔；按 ESC 或空格可随时急停。
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
        <p v-if="pauseGestureHint" class="banner banner-info">{{ pauseGestureHint }}</p>
      </div>

      <ErrorBanner v-if="taskStore.error" kind="error">{{ taskStore.error }}</ErrorBanner>
      <GuideCard
        v-if="dirtySkipWarning"
        badge="!"
        title="画布上有旧墨迹，本次跳过了工具条自动学习"
      >
        {{ dirtySkipWarning.message }}
        <div class="warn-actions">
          <button class="btn btn-primary btn-sm" :disabled="!canClear" @click="clearAsk = 'confirm'">
            清空画布，从头绘制
          </button>
          <button class="btn btn-sm" @click="emit('goto', 'calibrate')">去校准页</button>
        </div>
      </GuideCard>
      <GuideCard
        v-if="probeFailWarning"
        badge="!"
        title="工具条自动学习未完成"
        action="去校准页补标"
        @action="gotoCalibrateWith('slider')"
      >
        {{ probeFailWarning.message }}补标后，笔宽自适应与绘制后自动清空都会恢复工作。
      </GuideCard>
      <ErrorBanner v-for="(warn, i) in plainWarnings" :key="i" kind="warn">{{ warn.message }}</ErrorBanner>
      <ErrorBanner v-if="taskStore.undoFeedback" kind="warn">
        {{ taskStore.undoFeedback }}
        <button
          v-if="taskStore.undoFeedbackCode === 'E_UNDO_UNAVAILABLE'"
          class="btn btn-sm banner-action"
          @click="gotoCalibrateWith('undo')"
        >
          去校准页标撤销
        </button>
      </ErrorBanner>
      <ErrorBanner v-if="taskStore.clearFeedback" kind="warn">{{ taskStore.clearFeedback }}</ErrorBanner>
      <GuideCard
        v-if="clearAsk === 'confirm'"
        tone="urgent"
        badge="!"
        title="确认清空画布？"
        action="确认清空并从头绘制"
        @action="doClear(false)"
      >
        画板上的墨迹将被全部丢弃（程序会连点画板自己的撤销按钮直到画布干净），然后从第 1 笔重新开始绘制。
      </GuideCard>
      <GuideCard
        v-else-if="clearAsk === 'confirmDirty'"
        tone="urgent"
        badge="!"
        title="需要先学习工具条控件位置"
        action="授权并继续清空"
        @action="doClear(true)"
      >
        还没学到撤销按钮的位置。确认后程序将在画布有墨的状态下点击画布下方的工具条区域进行学习；画布上的墨迹将被全部丢弃。
      </GuideCard>
      <GuideCard
        v-if="deleteAsk"
        tone="urgent"
        badge="!"
        title="确认删除当前任务？"
        action="确认删除任务"
        @action="onDeleteTask"
      >
        任务进度记录将被删除，本页回到「尚未开始绘制」；规划与校准保留，可随时重新开始。注意：画板上已画出的墨迹不会被清掉，需要一并清空请改用「清空画布，从头绘制」。
      </GuideCard>
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
        <button
          v-if="snapshot?.status === 'STOPPED'"
          class="btn"
          :disabled="taskStore.busy || taskStore.undoing"
          @click="taskStore.undo()"
        >
          {{ taskStore.undoing ? '回退中…' : '回退一笔' }}
        </button>
        <button class="btn" :disabled="!taskStore.canResume || taskStore.busy" @click="onResume">从断点续画</button>
        <button
          class="btn"
          :disabled="!canClear"
          :title="snapshot?.status === 'STOPPED' ? '清空画板上的旧墨迹，然后从第 1 笔重新绘制' : undefined"
          @click="clearAsk = 'confirm'"
        >
          {{ taskStore.clearing ? '清空中…' : '清空画布，从头绘制' }}
        </button>
        <button
          v-if="snapshot"
          class="btn btn-danger"
          :disabled="taskStore.isActive || taskStore.busy"
          title="删除任务进度记录，回到未开始绘制的初始状态（规划与校准保留，画板墨迹不受影响）"
          @click="deleteAsk = true"
        >
          删除任务
        </button>
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

/* 引导卡内的多动作行（清空 / 去校准页并存时） */
.warn-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 8px;
}

/* 反馈 banner 内联的引导按钮 */
.banner-action {
  margin-left: 8px;
  vertical-align: baseline;
}
</style>
