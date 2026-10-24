<script setup lang="ts">
import { computed, nextTick, onMounted, shallowRef, useTemplateRef, watch } from 'vue'
import RectEditor from '@/components/RectEditor.vue'
import type { PlanSummary, Rect } from '@/api/types'
import { useEditStore } from '@/stores/edit'

const props = defineProps<{ summary: PlanSummary; pending?: boolean }>()
const emit = defineEmits<{ (event: 'settings'): void }>()

const editStore = useEditStore()

const instruction = shallowRef('')
const roi = shallowRef<Rect | null>(null)
const selecting = shallowRef(false)
// 重规划期间的编辑请求排队，pending 结束后自动执行；hint 保证点击必有可见反馈。
const queued = shallowRef<string | null>(null)
const hint = shallowRef<string | null>(null)
const showHistory = shallowRef(false)
const compareEl = useTemplateRef<HTMLDivElement>('compareEl')

const CHIPS = [
  { label: '上移', text: '上移40' },
  { label: '下移', text: '下移40' },
  { label: '左移', text: '左移40' },
  { label: '右移', text: '右移40' },
  { label: '放大', text: '放大1.2倍' },
  { label: '缩小', text: '缩小0.8倍' },
  { label: '加粗', text: '加粗1' },
  { label: '变细', text: '变细1' },
  { label: '镜像', text: '镜像' },
  { label: '旋转90°', text: '旋转90' },
]

const busy = computed(() => editStore.busy)
const draft = computed(() => editStore.draft)

function request(text: string) {
  const value = text.trim()
  if (!value || editStore.busy) return
  if (props.pending === true) {
    queued.value = value
    hint.value = '正在重新规划预览，完成后将自动生成草稿…'
    return
  }
  hint.value = null
  editStore.create(value, selecting.value ? roi.value : null)
}

watch(
  () => [props.pending, editStore.busy] as const,
  ([pending, editing]) => {
    if (editing) {
      queued.value = null
      hint.value = null
      return
    }
    if (!pending && queued.value !== null) {
      const text = queued.value
      queued.value = null
      hint.value = null
      editStore.create(text, selecting.value ? roi.value : null)
    }
  },
)

// 草稿在页面下方，生成后滚动进视口，避免用户以为“没有反应”。
watch(draft, (d) => {
  if (d) void nextTick(() => compareEl.value?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }))
})

function submit() {
  request(instruction.value)
}

function useChip(text: string) {
  instruction.value = text
  request(text)
}

function clearRoi() {
  roi.value = null
  selecting.value = false
}

function toggleSelecting() {
  selecting.value = !selecting.value
  if (!selecting.value) roi.value = null
}

const versionHistory = computed(() => editStore.versions.filter((v) => !v.current && v.available))
const hasHistory = computed(() => versionHistory.value.length > 0)
const projectVersion = computed(() => editStore.versions.find((v) => v.current)?.version ?? '?')

function toggleHistory() {
  showHistory.value = !showHistory.value
  confirmRevert.value = null
  if (showHistory.value) void editStore.refreshVersions()
}

// 内置/嵌入浏览器不支持 window.confirm（原生对话框会导致页面崩溃），改为行内二次确认。
const confirmRevert = shallowRef<number | null>(null)

function goRevert(version: number) {
  if (confirmRevert.value !== version) {
    confirmRevert.value = version
    return
  }
  confirmRevert.value = null
  showHistory.value = false
  void editStore.revert(version)
}

onMounted(() => {
  void editStore.refreshVersions()
})
</script>

<template>
  <div class="edit-bar card">
    <div class="bar-row">
      <span class="bar-title">AI 编辑</span>
      <input
        v-model="instruction"
        class="instruction"
        type="text"
        maxlength="500"
        placeholder="用一句话描述修改，如：把图案上移40、选区内擦除、加粗1"
        :disabled="busy"
        @keyup.enter="submit"
      />
      <button
        class="btn btn-sm"
        :class="{ active: selecting }"
        :disabled="busy"
        title="开启后在掩膜上框选局部区域，编辑只作用于选区"
        @click="toggleSelecting"
      >
        {{ selecting ? '框选中' : '框选区域' }}
      </button>
      <button
        v-if="hasHistory"
        class="btn btn-sm"
        :class="{ active: showHistory }"
        :disabled="busy || editStore.reverting"
        title="查看源图版本历史并复原"
        @click="toggleHistory"
      >
        {{ editStore.reverting ? '复原中…' : '复原' }}
      </button>
      <button class="btn btn-sm btn-primary" :disabled="!instruction.trim() || busy" @click="submit">
        {{ busy ? '生成草稿…' : props.pending ? '排队生成' : '生成草稿' }}
      </button>
    </div>

    <div v-if="showHistory && hasHistory" class="history">
      <div class="history-head num">
        当前 v{{ projectVersion }} · 历史版本（复原会把所选版本另存为新版本，可反复来回）
      </div>
      <div v-for="item in versionHistory" :key="item.version" class="history-row">
        <span class="history-label">
          <b class="num">v{{ item.version }}</b>
          <span class="history-text">{{ item.label }}</span>
        </span>
        <button
          class="btn btn-sm"
          :class="{ 'btn-primary': confirmRevert === item.version }"
          :disabled="busy || editStore.reverting"
          @click="goRevert(item.version)"
        >
          {{ confirmRevert === item.version ? '再次点击确认复原' : '复原到此版本' }}
        </button>
      </div>
    </div>

    <div class="chips">
      <button v-for="chip in CHIPS" :key="chip.label" class="chip" :disabled="busy" @click="useChip(chip.text)">
        {{ chip.label }}
      </button>
      <button v-if="roi" class="chip chip-roi" @click="clearRoi">清除选区</button>
    </div>

    <p v-if="hint" class="edit-hint">{{ hint }}</p>

    <div v-if="selecting" class="roi-stage">
      <RectEditor
        v-model="roi"
        :image-width="summary.targetSize.w"
        :image-height="summary.targetSize.h"
      >
        <img :src="summary.assets.target" class="roi-base" alt="框选基准（目标掩膜）" draggable="false" />
      </RectEditor>
      <p class="roi-tip">在目标掩膜上按住拖动圈定编辑范围；编辑结果与预览所见同一坐标系。</p>
    </div>

    <p v-if="editStore.error" class="edit-error">
      {{ editStore.error }}
      <template v-if="editStore.notUnderstood">可换用「上移/下移/放大/缩小/镜像/加粗/变细/旋转/擦除/裁剪」等指令，或先用「框选区域」限定范围。</template>
      <button v-if="editStore.needsSettings" class="link-btn" @click="emit('settings')">去设置</button>
    </p>

    <div v-if="draft" ref="compareEl" class="compare">
      <div class="pane">
        <div class="pane-label">当前版本</div>
        <img :src="summary.assets.target" alt="当前版本" />
      </div>
      <div class="pane">
        <div class="pane-label">
          {{ draft.diffUrl ? '编辑草稿（改动处标红）' : '生成草稿（云端模型输出）' }}
        </div>
        <img :src="draft.diffUrl ?? draft.draftUrl" :key="draft.editId" alt="编辑草稿" />
      </div>
      <div class="decision">
        <div v-if="draft.parsedOps?.length" class="parsed num">
          理解为：{{ draft.parsedOps.map((op) => JSON.stringify(op)).join('；') }}
        </div>
        <div class="decision-actions">
          <button class="btn btn-sm" :disabled="editStore.busy" @click="editStore.discard()">放弃</button>
          <button class="btn btn-sm btn-primary" :disabled="editStore.busy" @click="editStore.apply()">
            {{ editStore.busy ? '应用中…' : '应用并重规划' }}
          </button>
        </div>
        <p class="decision-tip">应用后源图产生新版本并自动重新规划，不满意可放弃本次草稿。</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.edit-bar {
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.bar-row {
  display: flex;
  align-items: center;
  gap: 8px;
  /* 窄屏：输入框与按钮换行，按钮行独占一行 */
  flex-wrap: wrap;
}

.bar-title {
  font-size: 12px;
  color: var(--gold-soft);
  letter-spacing: 0.08em;
  white-space: nowrap;
}

.instruction {
  flex: 1;
  min-width: 180px;
  background: var(--ink-0);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  color: var(--text);
  padding: 7px 10px;
  font-size: 13px;
}

.instruction:focus {
  outline: none;
  border-color: var(--gold);
}

/* scoped 特异性高于全局触摸规则，单独保证 iOS 聚焦不放大 */
@media (pointer: coarse), (max-width: 1376px) {
  .instruction {
    font-size: 16px;
  }

  .roi-tip,
  .history-head,
  .pane-label,
  .parsed,
  .decision-tip {
    font-size: 12px;
  }
}

.btn.active {
  border-color: var(--gold);
  color: var(--gold);
  background: var(--gold-dim);
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.chip {
  padding: 3px 10px;
  font-size: 12px;
  border-radius: 999px;
  border: 1px solid var(--line-soft);
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
}

.chip:hover:not(:disabled) {
  border-color: var(--gold);
  color: var(--gold);
}

.chip-roi {
  color: var(--red, #d62828);
  border-color: rgba(214, 40, 40, 0.4);
}

.roi-stage :deep(.stage) {
  max-width: 320px;
  border: 1px solid var(--line-soft);
}

.roi-base {
  width: 100%;
  display: block;
  background: #fff;
}

.roi-tip {
  margin: 6px 0 0;
  font-size: 11px;
  color: var(--text-faint);
}

.edit-error {
  margin: 0;
  font-size: 12px;
  color: var(--red, #e0655f);
}

.edit-hint {
  margin: 0;
  font-size: 12px;
  color: var(--gold-soft);
}

.history {
  display: flex;
  flex-direction: column;
  gap: 4px;
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
}

.history-head {
  font-size: 11px;
  color: var(--text-faint);
}

.history-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.history-label {
  display: flex;
  align-items: baseline;
  gap: 6px;
  min-width: 0;
  font-size: 12px;
  color: var(--text-dim);
}

.history-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.link-btn {
  border: none;
  background: transparent;
  color: var(--gold);
  font-size: 12px;
  cursor: pointer;
  padding: 0 2px;
  text-decoration: underline;
}

.compare {
  display: grid;
  grid-template-columns: 1fr 1fr minmax(180px, 240px);
  gap: 10px;
  align-items: start;
}

@media (max-width: 1023px) {
  .compare {
    grid-template-columns: 1fr 1fr;
  }

  .decision {
    grid-column: 1 / -1;
  }
}

.pane {
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-sm);
  overflow: hidden;
}

.pane-label {
  font-size: 11px;
  color: var(--text-dim);
  padding: 5px 8px;
  border-bottom: 1px solid var(--line-soft);
}

.pane img {
  display: block;
  width: 100%;
  max-height: 240px;
  object-fit: contain;
  background: #fff;
}

.decision {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.parsed {
  font-size: 11px;
  color: var(--text-dim);
  word-break: break-all;
  max-height: 72px;
  overflow: auto;
}

.decision-actions {
  display: flex;
  gap: 8px;
}

.decision-tip {
  margin: 0;
  font-size: 11px;
  color: var(--text-faint);
}
</style>
