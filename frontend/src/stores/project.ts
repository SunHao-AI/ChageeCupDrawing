import { defineStore } from 'pinia'
import { shallowRef, computed } from 'vue'
import * as api from '@/api/endpoints'
import type { Calibration, Project, WindowInfo } from '@/api/types'
import { errorMessage } from '@/utils/error'

/** 默认选中的目标窗口标题关键字（小程序名）。 */
export const TARGET_WINDOW_KEYWORD = '霸王茶姬'

export const useProjectStore = defineStore('project', () => {
  const project = shallowRef<Project | null>(null)
  const calibration = shallowRef<Calibration | null>(null)
  const windows = shallowRef<WindowInfo[]>([])
  const selectedHwnd = shallowRef<number | null>(null)
  const busy = shallowRef(false)
  const error = shallowRef<string | null>(null)
  // 用户在下拉里手动选过之后，不再自动改选。
  const userPicked = shallowRef(false)

  const calibrated = computed(
    () => calibration.value?.rect != null && calibration.value.rect.w > 0 && calibration.value.rect.h > 0,
  )
  const selectedWindow = computed(
    () => windows.value.find((w) => w.hwnd === selectedHwnd.value) ?? null,
  )
  const captureRect = computed(() => calibration.value?.rect ?? null)

  /** 绘制目标是霸王茶姬小程序的画板窗口。 */
  const targetWindow = computed(
    () => windows.value.find((w) => w.title.includes(TARGET_WINDOW_KEYWORD)) ?? null,
  )

  async function createProject(file: File, name?: string) {
    busy.value = true
    error.value = null
    try {
      project.value = await api.createProject(file, name)
      // 换图后旧校准与旧规划失效。
      calibration.value = null
      return project.value
    } catch (e) {
      error.value = errorMessage(e)
      throw e
    } finally {
      busy.value = false
    }
  }

  const MAX_UPLOAD_BYTES = 20 * 1024 * 1024

  /** 导入前本地校验（类型/大小），通过则上传。文件对话框与拖拽共用。 */
  async function importFile(file: File) {
    if (!['image/png', 'image/jpeg'].includes(file.type)) {
      error.value = '仅支持 PNG 或 JPEG 图片'
      return null
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      error.value = '图片超过 20 MB 上限'
      return null
    }
    return createProject(file)
  }

  async function refreshWindows() {
    error.value = null
    try {
      windows.value = await api.listWindows()
      if (userPicked.value) return
      // 默认选中霸王茶姬窗口；找不到则保持未选，由校准页提示用户打开小程序。
      const target = windows.value.find((w) => w.title.includes(TARGET_WINDOW_KEYWORD))
      selectedHwnd.value = target?.hwnd ?? null
    } catch (e) {
      error.value = errorMessage(e)
    }
  }

  /** 校准（auto/manual 共用），成功后写入 calibration。 */
  async function runCalibration(mode: 'auto' | 'manual', rect?: Calibration['rect']) {
    if (!project.value || selectedHwnd.value === null) return null
    busy.value = true
    error.value = null
    try {
      const result = await api.calibrate(project.value.projectId, {
        hwnd: selectedHwnd.value,
        mode,
        rect: rect ?? undefined,
      })
      if (result.rect) calibration.value = result
      else calibration.value = { ...result } // confidence=0 也要回显截图尺寸
      return result
    } catch (e) {
      error.value = errorMessage(e)
      throw e
    } finally {
      busy.value = false
    }
  }

  function reset() {
    project.value = null
    calibration.value = null
    error.value = null
  }

  return {
    project,
    calibration,
    windows,
    selectedHwnd,
    selectedWindow,
    targetWindow,
    userPicked,
    captureRect,
    calibrated,
    busy,
    error,
    createProject,
    importFile,
    refreshWindows,
    runCalibration,
    reset,
  }
})
