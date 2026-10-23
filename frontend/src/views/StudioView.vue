<script setup lang="ts">
import { computed, onMounted, shallowRef, useTemplateRef, watch } from 'vue'
import AssetViewer from '@/components/AssetViewer.vue'
import EditBar from '@/components/EditBar.vue'
import ErrorBanner from '@/components/ErrorBanner.vue'
import GuideCard from '@/components/GuideCard.vue'
import MetricStrip from '@/components/MetricStrip.vue'
import ParamField from '@/components/ParamField.vue'
import { lineArtProviders } from '@/api/endpoints'
import { PLAN_GROUPS } from '@/params/registry'
import { useEditStore } from '@/stores/edit'
import { useParamsStore } from '@/stores/params'
import { usePlanStore } from '@/stores/plan'
import { usePresetStore } from '@/stores/preset'
import { useProjectStore } from '@/stores/project'
import type { ViewEmits } from './types'

const emit = defineEmits<ViewEmits>()
const paramsStore = useParamsStore()
const planStore = usePlanStore()
const projectStore = useProjectStore()
const presetStore = usePresetStore()
const editStore = useEditStore()

const fileInput = useTemplateRef<HTMLInputElement>('fileInput')
const saveError = shallowRef<string | null>(null)
const savedOk = shallowRef(false)
const aiAvailable = shallowRef(false)

/** 条件字段：strategy 切换时隐藏不相关项，但值保留在 params 里。 */
function visible(meta: (typeof PLAN_GROUPS)[number]['fields'][number]): boolean {
  return meta.when ? meta.when(paramsStore.plan) : true
}

// 改动即预览：任何 plan 字段变化都去抖重规划（store 内部判门禁与在途取消）。
watch(
  () => ({ ...paramsStore.plan }),
  () => {
    if (projectStore.calibrated && projectStore.project) planStore.schedule()
  },
  { deep: false },
)

// 校准完成但还没有规划结果时，自动生成第一版预览。
// immediate 覆盖「在校准页完成校准后才切回工作台」的挂载时机。
watch(
  () => projectStore.calibrated,
  (ok) => {
    if (ok && projectStore.project && planStore.summary === null) planStore.schedule()
  },
  { immediate: true },
)

function pickFile() {
  fileInput.value?.click()
}

async function onFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  try {
    await projectStore.importFile(file)
    editStore.clear()
    planStore.clear()
  } catch {
    /* 错误已写入 projectStore.error */
  }
}

/** 拖拽导入：空态区域接收文件，复用与对话框一致的校验。 */
const dropping = shallowRef(false)

function onDragOver(event: DragEvent) {
  event.preventDefault()
  dropping.value = true
}

function onDragLeave() {
  dropping.value = false
}

async function onDrop(event: DragEvent) {
  event.preventDefault()
  dropping.value = false
  const file = event.dataTransfer?.files?.[0]
  if (!file) return
  try {
    await projectStore.importFile(file)
    editStore.clear()
    planStore.clear()
  } catch {
    /* 错误已写入 projectStore.error */
  }
}

async function savePreset() {
  saveError.value = null
  savedOk.value = false
  const name = window.prompt('预设名称（1–64 字符）')?.trim()
  if (!name) return
  try {
    await presetStore.save(name, { ...paramsStore.plan })
    savedOk.value = true
    window.setTimeout(() => (savedOk.value = false), 2500)
  } catch (e) {
    saveError.value = e instanceof Error ? e.message : '保存失败'
  }
}

const previewReady = computed(() => planStore.summary !== null)

// 墨量异常：整图被判成墨（典型：暗调照片 + fidelity 全局阈值），绘制会变成实心黑块。
const inkTooHigh = computed(() => (planStore.summary?.inkRatio ?? 0) > 0.4)
const inkAdviceAdaptive = computed(() => paramsStore.plan.imageMode !== 'adaptive')

function fixInk() {
  paramsStore.setPlan({ imageMode: 'adaptive' })
}

onMounted(() => {
  lineArtProviders()
    .then((list) => (aiAvailable.value = list.length > 0))
    .catch(() => (aiAvailable.value = false))
  editStore.checkProviders()
})
</script>

<template>
  <div class="studio">
    <input ref="fileInput" type="file" accept="image/png,image/jpeg" hidden @change="onFile" />

    <aside class="card panel scroll">
      <div class="panel-head">
        <h3 class="card-title">原图</h3>
        <button class="btn btn-sm" :disabled="projectStore.busy" @click="pickFile">
          {{ projectStore.project ? '更换图片' : '导入图片' }}
        </button>
      </div>

      <div
        class="source-zone"
        :class="{ dropping }"
        @dragover="onDragOver"
        @dragleave="onDragLeave"
        @drop="onDrop"
      >
        <div v-if="projectStore.project" class="source">
          <img :src="projectStore.project.imageUrl" alt="原图" />
          <div class="source-meta num">
            {{ projectStore.project.name }} · {{ projectStore.project.width }}×{{ projectStore.project.height }}
          </div>
        </div>
        <div v-else class="source-empty">
          <p>拖拽图片到此处，或点击上方「导入图片」</p>
          <p class="sub">支持 PNG / JPEG，≤20 MB</p>
        </div>
      </div>

      <ErrorBanner v-if="projectStore.error" kind="error">{{ projectStore.error }}</ErrorBanner>
      <GuideCard
        v-if="projectStore.project && !projectStore.calibrated"
        badge="2"
        title="下一步：校准画布"
        action="去校准"
        @action="emit('goto', 'calibrate')"
      >
        先在电脑上打开目标窗口（小程序画板页），再到校准页选择该窗口并识别白色画布。
      </GuideCard>

      <div class="divider"></div>
      <h3 class="card-title">规划参数</h3>

      <section v-for="group in PLAN_GROUPS" :key="group.name" class="group">
        <h4 class="group-name">{{ group.name }}</h4>
        <template v-for="meta in group.fields" :key="meta.key">
          <ParamField
            v-if="visible(meta)"
            :meta="meta"
            :model-value="(paramsStore.plan as any)[meta.key]"
            @update:model-value="paramsStore.setPlan({ [meta.key]: $event } as any)"
          />
        </template>
      </section>

      <div class="divider"></div>
      <div class="preset-actions">
        <button class="btn btn-sm" :disabled="!previewReady || presetStore.busy" @click="savePreset">
          保存为预设
        </button>
        <button class="btn btn-sm" @click="emit('goto', 'presets')">管理预设</button>
        <button class="btn btn-sm" :disabled="aiAvailable" :title="aiAvailable ? '' : 'AI 线稿即将上线'">
          AI 线稿
        </button>
      </div>
      <div v-if="savedOk" class="saved-ok">已保存到预设</div>
      <ErrorBanner v-if="saveError" kind="error">{{ saveError }}</ErrorBanner>
    </aside>

    <section class="preview">
      <ErrorBanner v-if="planStore.error" kind="error">{{ planStore.error }}</ErrorBanner>

      <template v-if="previewReady && planStore.summary">
        <GuideCard
          v-if="inkTooHigh"
          tone="urgent"
          badge="!"
          :title="inkAdviceAdaptive ? '墨量过高：绘制会变成实心黑块' : '墨量过高：建议下调二值阈值'"
          :action="inkAdviceAdaptive ? '改用自适应模式' : undefined"
          @action="fixInk"
        >
          <template v-if="inkAdviceAdaptive">
            当前墨迹占比 {{ ((planStore.summary.inkRatio ?? 0) * 100).toFixed(0) }}%，多半是暗调照片配「保真」全局阈值导致整图被判成墨。建议改用「自适应」线稿模式。
          </template>
          <template v-else>
            当前墨迹占比 {{ ((planStore.summary.inkRatio ?? 0) * 100).toFixed(0) }}%，请把「二值阈值」调低到暗部细节附近。
          </template>
        </GuideCard>
        <MetricStrip :metrics="planStore.summary.metrics" :work-scale="planStore.summary.workScale" />
        <AssetViewer :summary="planStore.summary" :pending="planStore.pending" />
        <EditBar
          v-if="editStore.available"
          :summary="planStore.summary"
          :pending="planStore.pending"
        />
        <div class="run-bar">
          <span class="run-hint num">
            {{ planStore.summary.strategy }} · {{ planStore.summary.metrics.pathCount }} 笔 ·
            目标 {{ planStore.summary.targetSize.w }}×{{ planStore.summary.targetSize.h }}
          </span>
          <button class="btn btn-primary" @click="emit('goto', 'run')">去绘制</button>
        </div>
      </template>

      <div v-else class="preview-empty card">
        <template v-if="!projectStore.project">导入一张图片开始</template>
        <template v-else-if="!projectStore.calibrated">完成校准后自动规划预览</template>
        <template v-else>正在准备第一版预览…</template>
      </div>
    </section>
  </div>
</template>

<style scoped>
.studio {
  display: grid;
  grid-template-columns: 340px minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}

.panel {
  max-height: calc(100vh - 108px);
}

.panel-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
}

.panel-head .card-title {
  margin: 0;
}

.source-zone {
  border-radius: var(--radius-sm);
  transition: box-shadow 0.15s, outline-color 0.15s;
  outline: 2px dashed transparent;
  outline-offset: 4px;
}

.source-zone.dropping {
  outline-color: var(--gold);
}

.source img {
  width: 100%;
  border-radius: var(--radius-sm);
  border: 1px solid var(--line-soft);
  display: block;
}

.source-meta {
  font-size: 11px;
  color: var(--text-faint);
  margin-top: 6px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.source-empty {
  border: 1px dashed var(--line);
  border-radius: var(--radius-sm);
  padding: 24px;
  text-align: center;
  color: var(--text-faint);
  font-size: 12px;
}

.source-empty .sub {
  margin-top: 6px;
  color: var(--text-faint);
}

.divider {
  height: 1px;
  background: var(--line-soft);
  margin: 14px 0;
}

.group {
  margin-bottom: 6px;
}

.group-name {
  margin: 0 0 8px;
  font-size: 11px;
  color: var(--gold-soft);
  letter-spacing: 0.1em;
}

.preset-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.saved-ok {
  margin-top: 8px;
  font-size: 12px;
  color: var(--green);
}

.preview {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.preview-empty {
  height: 420px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-faint);
}

.run-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.run-hint {
  color: var(--text-dim);
  font-size: 12px;
}
</style>
