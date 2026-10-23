<script setup lang="ts">
import { computed, onMounted, onUnmounted, shallowRef } from 'vue'
import { health } from '@/api/endpoints'
import StepperBar from '@/components/StepperBar.vue'
import { useProjectStore } from '@/stores/project'
import type { ViewKey } from '@/views/types'
import StudioView from '@/views/StudioView.vue'
import CalibrateView from '@/views/CalibrateView.vue'
import RunView from '@/views/RunView.vue'
import PresetsView from '@/views/PresetsView.vue'

const VIEWS: { key: ViewKey; label: string }[] = [
  { key: 'studio', label: '工作台' },
  { key: 'calibrate', label: '校准' },
  { key: 'run', label: '绘制' },
  { key: 'presets', label: '预设' },
]

const projectStore = useProjectStore()
const current = shallowRef<ViewKey>('studio')
const backend = shallowRef<{ ok: boolean; version: string }>({ ok: false, version: '' })

// 直接双击 dist/index.html 打开时 fetch('/api/…') 会解析到 file:// 而失败。
const fileProtocol = window.location.protocol === 'file:'

const currentComponent = computed(
  () => ({ studio: StudioView, calibrate: CalibrateView, run: RunView, presets: PresetsView })[current.value],
)

function onGoto(view: ViewKey) {
  current.value = view
}

// 全局拦截：拖文件落到非投放区时阻止浏览器导航到 file://（表现为 404/白页）。
// 投放区自身的 drop 处理器照常工作，这里只是兜底 preventDefault。
function blockFileNavigation(event: DragEvent) {
  if (event.dataTransfer?.types?.includes('Files')) event.preventDefault()
}

async function ping() {
  try {
    const result = await health()
    backend.value = { ok: result.status === 'ok', version: result.version }
  } catch {
    backend.value = { ok: false, version: '' }
  }
}

onMounted(() => {
  ping()
  projectStore.refreshWindows()
  window.addEventListener('dragover', blockFileNavigation)
  window.addEventListener('drop', blockFileNavigation)
})

onUnmounted(() => {
  window.removeEventListener('dragover', blockFileNavigation)
  window.removeEventListener('drop', blockFileNavigation)
})
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div class="brand">
        <span class="brand-mark"></span>
        <span class="brand-name">杯贴绘制工作台</span>
        <span class="badge" :class="backend.ok ? 'badge-gold' : ''">
          {{ backend.ok ? `服务正常 v${backend.version}` : '后端未连接' }}
        </span>
      </div>
      <nav class="tabs">
        <button
          v-for="view in VIEWS"
          :key="view.key"
          class="tab"
          :class="{ active: current === view.key }"
          @click="current = view.key"
        >
          {{ view.label }}
        </button>
      </nav>
      <div class="project-chip">
        <template v-if="projectStore.project">
          <span class="project-name">{{ projectStore.project.name }}</span>
          <span class="badge" :class="projectStore.calibrated ? 'badge-gold' : ''">
            {{ projectStore.calibrated ? '已校准' : '未校准' }}
          </span>
        </template>
        <span v-else class="project-empty">尚未导入图片</span>
      </div>
    </header>

    <div v-if="fileProtocol" class="file-hint">
      正以 file:// 打开页面，接口请求无法送达后端。请在后端启动后访问
      <span class="num">http://127.0.0.1:8765</span>。
    </div>

    <StepperBar @goto="onGoto" />

    <main class="content scroll">
      <component :is="currentComponent" @goto="onGoto" />
    </main>
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.topbar {
  display: flex;
  align-items: center;
  gap: 24px;
  padding: 0 20px;
  height: 52px;
  border-bottom: 1px solid var(--line-soft);
  background: var(--ink-1);
  flex: none;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
}

.brand-mark {
  width: 14px;
  height: 14px;
  border-radius: 4px;
  background: linear-gradient(135deg, var(--gold) 0%, var(--gold-soft) 100%);
  box-shadow: 0 0 12px rgba(216, 169, 78, 0.4);
}

.brand-name {
  font-weight: 600;
  letter-spacing: 0.02em;
}

.tabs {
  display: flex;
  gap: 4px;
  flex: 1;
}

.tab {
  border: none;
  background: transparent;
  color: var(--text-dim);
  padding: 6px 14px;
  border-radius: var(--radius-sm);
  font-size: 13px;
  cursor: pointer;
}

.tab:hover {
  color: var(--text);
}

.tab.active {
  background: var(--gold-dim);
  color: var(--gold);
}

.project-chip {
  display: flex;
  align-items: center;
  gap: 8px;
}

.project-name {
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-dim);
}

.project-empty {
  color: var(--text-faint);
  font-size: 12px;
}

.content {
  flex: 1;
  padding: 18px 20px 28px;
}

.file-hint {
  background: rgba(217, 164, 65, 0.1);
  border-bottom: 1px solid rgba(217, 164, 65, 0.4);
  color: #ecc883;
  padding: 8px 20px;
  font-size: 13px;
}

.file-hint .num {
  color: var(--gold);
}
</style>
