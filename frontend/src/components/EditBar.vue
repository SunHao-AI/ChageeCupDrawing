<script setup lang="ts">
import { computed, shallowRef } from 'vue'
import RectEditor from '@/components/RectEditor.vue'
import type { PlanSummary, Rect } from '@/api/types'
import { useEditStore } from '@/stores/edit'

const props = defineProps<{ summary: PlanSummary; pending?: boolean }>()

const editStore = useEditStore()

const instruction = shallowRef('')
const roi = shallowRef<Rect | null>(null)
const selecting = shallowRef(false)

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

const busy = computed(() => editStore.busy || props.pending === true)
const draft = computed(() => editStore.draft)

function submit() {
  const text = instruction.value.trim()
  if (!text || busy.value) return
  editStore.create(text, selecting.value ? roi.value : null)
}

function useChip(text: string) {
  instruction.value = text
  submit()
}

function clearRoi() {
  roi.value = null
  selecting.value = false
}

function toggleSelecting() {
  selecting.value = !selecting.value
  if (!selecting.value) roi.value = null
}
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
      <button class="btn btn-sm btn-primary" :disabled="!instruction.trim() || busy" @click="submit">
        {{ editStore.busy ? '生成草稿…' : '生成草稿' }}
      </button>
    </div>

    <div class="chips">
      <button v-for="chip in CHIPS" :key="chip.label" class="chip" :disabled="busy" @click="useChip(chip.text)">
        {{ chip.label }}
      </button>
      <button v-if="roi" class="chip chip-roi" @click="clearRoi">清除选区</button>
    </div>

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
    </p>

    <div v-if="draft" class="compare">
      <div class="pane">
        <div class="pane-label">当前版本</div>
        <img :src="summary.assets.target" alt="当前版本" />
      </div>
      <div class="pane">
        <div class="pane-label">编辑草稿（改动处标红）</div>
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
}

.bar-title {
  font-size: 12px;
  color: var(--gold-soft);
  letter-spacing: 0.08em;
  white-space: nowrap;
}

.instruction {
  flex: 1;
  min-width: 0;
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

.compare {
  display: grid;
  grid-template-columns: 1fr 1fr minmax(180px, 240px);
  gap: 10px;
  align-items: start;
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
