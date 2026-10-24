import { defineStore } from 'pinia'
import { shallowRef, computed } from 'vue'
import * as api from '@/api/endpoints'
import type {
  AndroidDevice,
  Calibration,
  DeviceKind,
  DeviceList,
  MirrorStatus,
  Project,
  Rect,
  WindowInfo,
} from '@/api/types'
import { errorMessage } from '@/utils/error'

/** 默认选中的目标窗口标题关键字（小程序名）。 */
export const TARGET_WINDOW_KEYWORD = '霸王茶姬'

export const useProjectStore = defineStore('project', () => {
  const project = shallowRef<Project | null>(null)
  const calibration = shallowRef<Calibration | null>(null)
  const windows = shallowRef<WindowInfo[]>([])
  const android = shallowRef<AndroidDevice[]>([])
  const mirror = shallowRef<MirrorStatus | null>(null)
  const tools = shallowRef<DeviceList['tools']>({ available: false, missing: [] })
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

  /** 绘制目标是霸王茶姬小程序的画板窗口（PC）或投屏窗口（手机）。 */
  const targetWindow = computed(
    () => windows.value.find((w) => w.title.includes(TARGET_WINDOW_KEYWORD)) ?? null,
  )

  /** 选中的窗口是否就是当前投屏窗口：决定校准写入的 deviceKind。 */
  const isMirrorTarget = computed(
    () => mirror.value?.running === true && mirror.value.hwnd === selectedHwnd.value,
  )
  const deviceKind = computed<DeviceKind>(() => (isMirrorTarget.value ? 'mirror' : 'pc'))

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
      const devices = await api.listDevices()
      windows.value = devices.pcWindows
      android.value = devices.android
      mirror.value = devices.mirror
      tools.value = devices.tools
      if (userPicked.value) return
      // 优先选中已登记的投屏窗口；否则回到霸王茶姬窗口；都没有则保持未选。
      if (devices.mirror.running) selectedHwnd.value = devices.mirror.hwnd
      else selectedHwnd.value = targetWindow.value?.hwnd ?? null
    } catch (e) {
      error.value = errorMessage(e)
    }
  }

  /** 启动 scrcpy 投屏并把它设为绘制目标；工具缺失时抛出后端提示。 */
  async function startMirror(serial: string) {
    busy.value = true
    error.value = null
    try {
      mirror.value = await api.startMirror(serial)
      if (mirror.value.running) {
        if (!windows.value.some((w) => w.hwnd === mirror.value?.hwnd)) await refreshWindows()
        userPicked.value = true
        selectedHwnd.value = mirror.value.hwnd
      }
      return mirror.value
    } catch (e) {
      error.value = errorMessage(e)
      throw e
    } finally {
      busy.value = false
    }
  }

  /** 把已存在的窗口登记为投屏目标（外部启动的 scrcpy，或无手机时验证链路）。 */
  async function registerMirrorWindow(hwnd: number, serial: string, label = '') {
    busy.value = true
    error.value = null
    try {
      mirror.value = await api.registerMirror(hwnd, serial, label)
      userPicked.value = true
      selectedHwnd.value = hwnd
      return mirror.value
    } catch (e) {
      error.value = errorMessage(e)
      throw e
    } finally {
      busy.value = false
    }
  }

  async function unregisterMirror() {
    busy.value = true
    error.value = null
    try {
      mirror.value = await api.stopMirror()
      userPicked.value = false
      await refreshWindows()
    } catch (e) {
      error.value = errorMessage(e)
    } finally {
      busy.value = false
    }
  }

  /** 校准（auto/manual 共用），成功后写入 calibration。 */
  async function runCalibration(
    mode: 'auto' | 'manual',
    rect?: Calibration['rect'],
    extras?: { slider?: Rect; undo?: { x: number; y: number }; fold?: { x: number; y: number } },
  ) {
    if (!project.value || selectedHwnd.value === null) return null
    busy.value = true
    error.value = null
    try {
      const result = await api.calibrate(project.value.projectId, {
        hwnd: selectedHwnd.value,
        mode,
        rect: rect ?? undefined,
        deviceKind: deviceKind.value,
        deviceSerial: isMirrorTarget.value ? mirror.value?.serial ?? undefined : undefined,
        slider: extras?.slider,
        undo: extras?.undo,
        fold: extras?.fold,
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
    android,
    mirror,
    tools,
    selectedHwnd,
    selectedWindow,
    targetWindow,
    isMirrorTarget,
    deviceKind,
    userPicked,
    captureRect,
    calibrated,
    busy,
    error,
    createProject,
    importFile,
    refreshWindows,
    startMirror,
    registerMirrorWindow,
    unregisterMirror,
    runCalibration,
    reset,
  }
})
