import { defineStore } from 'pinia'
import { shallowRef } from 'vue'
import * as api from '@/api/endpoints'
import { ApiError } from '@/api/client'
import type { EditDraft, Rect, SourceVersion } from '@/api/types'
import { errorMessage } from '@/utils/error'
import { useParamsStore } from './params'
import { usePlanStore } from './plan'
import { useProjectStore } from './project'

/** 编辑草稿状态机：create → （对照）→ apply / discard。任何模型输出都必须经用户确认。 */
export const useEditStore = defineStore('edit', () => {
  const draft = shallowRef<EditDraft | null>(null)
  const busy = shallowRef(false)
  const error = shallowRef<string | null>(null)
  const notUnderstood = shallowRef(false)
  // 生成通道未配置 Key / 未授权上传：引导去设置页而非换说法。
  const needsSettings = shallowRef(false)
  const available = shallowRef(false)
  // 源图版本历史（复原用）。
  const versions = shallowRef<SourceVersion[]>([])
  const reverting = shallowRef(false)

  async function checkProviders() {
    try {
      available.value = (await api.editProviders()).length > 0
    } catch {
      available.value = false
    }
  }

  async function refreshVersions() {
    const project = useProjectStore().project
    if (!project) {
      versions.value = []
      return
    }
    try {
      versions.value = await api.listVersions(project.projectId)
    } catch {
      versions.value = []
    }
  }

  /** 复原到历史版本：内容另存为新版本并自动重规划；成功后可再次复原回来。 */
  async function revert(targetVersion: number) {
    const projectStore = useProjectStore()
    const project = projectStore.project
    if (!project || busy.value || reverting.value) return
    reverting.value = true
    error.value = null
    try {
      const result = await api.revertProject(project.projectId, targetVersion)
      projectStore.project = {
        ...project,
        imageUrl: `${result.imageUrl}?v=${result.sourceVersion}`,
        width: result.width,
        height: result.height,
        sourceVersion: result.sourceVersion,
      }
      draft.value = null
      const paramsStore = useParamsStore()
      if (paramsStore.plan.autoTrim) paramsStore.setPlan({ autoTrim: false })
      await usePlanStore().run(true)
      await refreshVersions()
    } catch (e) {
      error.value = errorMessage(e)
    } finally {
      reverting.value = false
    }
  }

  function clear() {
    draft.value = null
    error.value = null
    notUnderstood.value = false
    needsSettings.value = false
    versions.value = []
  }

  async function create(instruction: string, roi: Rect | null) {
    const projectStore = useProjectStore()
    const project = projectStore.project
    if (!project || busy.value) return
    busy.value = true
    error.value = null
    notUnderstood.value = false
    needsSettings.value = false
    try {
      // 旧草稿直接被新草稿顶替（后端草稿 7 天后自行回收）。
      const previous = draft.value
      draft.value = await api.createEdit(project.projectId, {
        instruction,
        roi: roi ?? undefined,
        baseVersion: project.sourceVersion ?? 1,
      })
      if (previous && previous.editId !== draft.value.editId) {
        api.discardEdit(previous.editId).catch(() => undefined)
      }
    } catch (e) {
      notUnderstood.value = e instanceof ApiError && e.code === 'E_EDIT_NOT_UNDERSTOOD'
      needsSettings.value =
        e instanceof ApiError &&
        (e.code === 'E_PROVIDER_NOT_CONFIGURED' ||
          e.code === 'E_CONSENT_REQUIRED' ||
          e.code === 'E_PROVIDER_AUTH')
      error.value = errorMessage(e)
    } finally {
      busy.value = false
    }
  }

  async function apply() {
    const current = draft.value
    if (!current || busy.value) return
    busy.value = true
    error.value = null
    try {
      const result = await api.applyEdit(current.editId)
      const projectStore = useProjectStore()
      if (projectStore.project) {
        projectStore.project = {
          ...projectStore.project,
          imageUrl: `${result.imageUrl}?v=${result.sourceVersion}`,
          width: result.width,
          height: result.height,
          sourceVersion: result.sourceVersion,
        }
      }
      draft.value = null
      // 语义草稿就是画布尺寸的掩膜：autoTrim 的裁边+重新居中会把位移/缩放
      // 编辑归零，应用时必须切到「不裁剪」保持编辑后的布局。
      const paramsStore = useParamsStore()
      if (current.channel === 'semantic' && paramsStore.plan.autoTrim) {
        paramsStore.setPlan({ autoTrim: false })
      }
      // 源图已换，同参数也必须重规划：绕过 plan store 的签名守卫。
      await usePlanStore().run(true)
      await refreshVersions()
    } catch (e) {
      error.value = errorMessage(e)
    } finally {
      busy.value = false
    }
  }

  async function discard() {
    const current = draft.value
    if (!current) return
    draft.value = null
    error.value = null
    try {
      await api.discardEdit(current.editId)
    } catch {
      /* 草稿丢失无副作用 */
    }
  }

  return {
    draft,
    busy,
    error,
    notUnderstood,
    needsSettings,
    available,
    versions,
    reverting,
    checkProviders,
    refreshVersions,
    revert,
    clear,
    create,
    apply,
    discard,
  }
})
