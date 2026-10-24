<script setup lang="ts">
import { computed, onMounted, shallowRef } from 'vue'
import ErrorBanner from '@/components/ErrorBanner.vue'
import { PLAN_GROUPS } from '@/params/registry'
import type { PlanParams, Preset } from '@/api/types'
import { usePresetStore } from '@/stores/preset'
import { useParamsStore } from '@/stores/params'
import type { ViewEmits } from './types'

const emit = defineEmits<ViewEmits>()
const presetStore = usePresetStore()
const paramsStore = useParamsStore()

const selectedId = shallowRef<string | null>(null)
const importInput = shallowRef<HTMLInputElement | null>(null)
const importError = shallowRef<string | null>(null)

const LABELS = new Map<string, string>()
for (const group of PLAN_GROUPS) {
  for (const field of group.fields) LABELS.set(field.key, field.label)
}

const selected = computed<Preset | null>(
  () => presetStore.items.find((p) => p.presetId === selectedId.value) ?? null,
)

/** 预设 vs 当前 plan 的逐字段差异（只列不同项）。 */
const diffs = computed(() => {
  const preset = selected.value
  if (!preset) return []
  const rows: { key: string; label: string; current: string; preset: string }[] = []
  for (const [key, value] of Object.entries(preset.params)) {
    const current = (paramsStore.plan as unknown as Record<string, unknown>)[key]
    if (JSON.stringify(current) !== JSON.stringify(value)) {
      rows.push({ key, label: LABELS.get(key) ?? key, current: String(current), preset: String(value) })
    }
  }
  return rows
})

const identical = computed(() => selected.value !== null && diffs.value.length === 0)

function apply(preset: Preset) {
  paramsStore.replacePlan({ ...preset.params })
  emit('goto', 'studio')
}

function exportPreset(preset: Preset) {
  const blob = new Blob([JSON.stringify({ name: preset.name, params: preset.params }, null, 2)], {
    type: 'application/json',
  })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${preset.name}.json`
  anchor.click()
  URL.revokeObjectURL(url)
}

async function onImport(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  importError.value = null
  if (!file) return
  try {
    const parsed = JSON.parse(await file.text()) as { name?: string; params?: PlanParams }
    if (!parsed.name || !parsed.params) throw new Error('缺少 name 或 params 字段')
    await presetStore.save(String(parsed.name).slice(0, 64), parsed.params)
  } catch (e) {
    importError.value = `导入失败：${e instanceof Error ? e.message : '文件不是合法预设 JSON'}`
  }
}

onMounted(() => presetStore.load())
</script>

<template>
  <div class="split presets" style="--split-side: 320px">
    <input ref="importInput" type="file" accept="application/json" hidden @change="onImport" />

    <aside class="card side scroll">
      <div class="side-head">
        <h3 class="card-title">预设列表</h3>
        <button class="btn btn-sm" @click="importInput?.click()">导入</button>
      </div>
      <ErrorBanner v-if="presetStore.error" kind="error">{{ presetStore.error }}</ErrorBanner>
      <ErrorBanner v-if="importError" kind="error">{{ importError }}</ErrorBanner>

      <p v-if="presetStore.items.length === 0" class="empty">还没有预设，在工作台调好参数后保存。</p>
      <button
        v-for="preset in presetStore.items"
        :key="preset.presetId"
        class="row"
        :class="{ active: selectedId === preset.presetId }"
        @click="selectedId = preset.presetId"
      >
        <span class="row-name">{{ preset.name }}</span>
        <span v-if="preset.builtin" class="badge">内置</span>
        <span v-else class="badge badge-gold">自定义</span>
      </button>
    </aside>

    <section class="card main">
      <template v-if="selected">
        <div class="main-head">
          <h3 class="card-title">{{ selected.name }} · 与当前参数对比</h3>
          <div class="actions">
            <button class="btn btn-sm" @click="exportPreset(selected)">导出 JSON</button>
            <button class="btn btn-primary btn-sm" @click="apply(selected)">套用到工作台</button>
          </div>
        </div>

        <div v-if="identical" class="banner banner-info">与当前工作台参数完全一致。</div>
        <table v-else class="diff">
          <thead>
            <tr><th>参数</th><th>当前</th><th>预设</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in diffs" :key="row.key">
              <td>{{ row.label }}</td>
              <td class="num cur">{{ row.current }}</td>
              <td class="num pre">{{ row.preset }}</td>
            </tr>
          </tbody>
        </table>
      </template>
      <div v-else class="placeholder">从左侧选择一个预设查看差异</div>
    </section>
  </div>
</template>

<style scoped>
.side {
  max-height: calc(100vh - 108px);
}

@media (max-width: 1023px) {
  .side {
    max-height: none;
  }
}

.side-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
}

.side-head .card-title {
  margin: 0;
}

.empty {
  color: var(--text-faint);
  font-size: 13px;
}

.row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  text-align: left;
  padding: 9px 10px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text);
  cursor: pointer;
  font-size: 13px;
}

.row:hover {
  background: var(--ink-2);
}

.row.active {
  background: var(--gold-dim);
  border-color: var(--gold-soft);
}

.row-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.main-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 14px;
}

.main-head .card-title {
  margin: 0;
}

.actions {
  display: flex;
  gap: 8px;
}

.placeholder {
  height: 320px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-faint);
}

.diff {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

/* 窄屏：参数对比表横向滚动，不压缩单元格 */
@media (max-width: 639px) {
  .diff {
    display: block;
    overflow-x: auto;
  }
}

.diff th {
  text-align: left;
  color: var(--text-faint);
  font-weight: 500;
  font-size: 12px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--line);
}

.diff td {
  padding: 8px 10px;
  border-bottom: 1px solid var(--line-soft);
}

.diff .cur {
  color: var(--text-dim);
}

.diff .pre {
  color: var(--gold);
}
</style>
