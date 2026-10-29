<script setup lang="ts">
import { computed, onMounted, onUnmounted, shallowRef } from 'vue'
import { getResumeState, health } from '@/api/endpoints'
import { ApiError } from '@/api/client'
import StepperBar from '@/components/StepperBar.vue'
import { usePlanStore } from '@/stores/plan'
import { useParamsStore } from '@/stores/params'
import { useProjectStore } from '@/stores/project'
import { useTaskStore } from '@/stores/task'
import type { ViewKey } from '@/views/types'
import { readLastProjectId, readLastView, writeLastProjectId, writeLastView } from '@/utils/session'
import StudioView from '@/views/StudioView.vue'
import CalibrateView from '@/views/CalibrateView.vue'
import RunView from '@/views/RunView.vue'
import PresetsView from '@/views/PresetsView.vue'
import SettingsView from '@/views/SettingsView.vue'
import { useAiStore } from '@/stores/ai'

const VIEWS: { key: ViewKey; label: string }[] = [
  { key: 'studio', label: '工作台' },
  { key: 'calibrate', label: '校准' },
  { key: 'run', label: '绘制' },
  { key: 'presets', label: '预设' },
  { key: 'settings', label: '设置' },
]

const aiStore = useAiStore()

const projectStore = useProjectStore()
const planStore = usePlanStore()
const paramsStore = useParamsStore()
const taskStore = useTaskStore()
const VIEW_KEYS: ViewKey[] = ['studio', 'calibrate', 'run', 'presets', 'settings']
const lastView = readLastView()
const current = shallowRef<ViewKey>(
  lastView && (VIEW_KEYS as string[]).includes(lastView) ? (lastView as ViewKey) : 'studio',
)
const backend = shallowRef<{ ok: boolean; version: string }>({ ok: false, version: '' })

// 直接双击 dist/index.html 打开时 fetch('/api/…') 会解析到 file:// 而失败。
const fileProtocol = window.location.protocol === 'file:'

const currentComponent = computed(
  () =>
    ({
      studio: StudioView,
      calibrate: CalibrateView,
      run: RunView,
      presets: PresetsView,
      settings: SettingsView,
    })[current.value],
)

function onGoto(view: ViewKey) {
  current.value = view
  writeLastView(view)
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

/** 按浏览器记住的项目 ID 恢复校准/规划/任务；项目已删除时清掉记忆即可。 */
async function restoreSession() {
  const projectId = readLastProjectId()
  if (!projectId) return
  try {
    const state = await getResumeState(projectId)
    // 恢复请求在途时用户已抢先导入新图：旧项目的恢复不得覆盖新项目。
    if (projectStore.project) return
    await projectStore.restore(state.project, state.calibration)
    if (state.plan) planStore.restore(state.plan)
    if (state.task) {
      // 执行参数跟随任务落库值，保证续画时面板显示与实际执行一致。
      paramsStore.replaceExec(state.task.exec)
      await taskStore.restore(state.task)
    }
  } catch (e) {
    // 只有「项目确实不存在」才清记忆；后端未就绪等瞬时失败保留记忆待下次重试。
    if (e instanceof ApiError && e.code === 'E_NOT_FOUND') writeLastProjectId(null)
  }
}

onMounted(() => {
  ping()
  aiStore.load()
  projectStore.refreshWindows()
  restoreSession()
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
        <img class="brand-mark" src="/chagee-logo.png" alt="霸王茶姬" />
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
      <Transition name="fade" mode="out-in">
        <component :is="currentComponent" :key="current" @goto="onGoto" />
      </Transition>
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
  width: 28px;
  height: 28px;
  border-radius: 50%;
  display: block;
  box-shadow: 0 0 14px rgba(226, 35, 26, 0.35);
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

/* 视图切换淡入淡出：短促克制，避免生硬跳变 */
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.14s ease, transform 0.14s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
  transform: translateY(4px);
}

@media (prefers-reduced-motion: reduce) {
  .fade-enter-active,
  .fade-leave-active {
    transition: none;
  }
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

/* 窄屏：顶栏换行——品牌/徽标一行，页签整行横向滑动；项目信息让位 */
@media (max-width: 1023px) {
  .topbar {
    flex-wrap: wrap;
    align-content: center;
    row-gap: 2px;
    height: auto;
    min-height: 52px;
    padding: 6px 12px;
  }

  .brand {
    flex: none;
  }

  .brand-name {
    display: none;
  }

  .project-chip {
    display: none;
  }

  .tabs {
    order: 3;
    flex: 1 0 100%;
    overflow-x: auto;
    scrollbar-width: none;
  }

  .tab {
    flex: none;
    white-space: nowrap;
  }

  .content {
    padding: 12px 12px 24px;
  }
}
</style>
