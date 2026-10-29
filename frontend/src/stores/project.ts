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
import { writeLastProjectId } from '@/utils/session'
import { useEditStore } from './edit'
import { useParamsStore } from './params'
import { usePlanStore } from './plan'
import { useTaskStore } from './task'

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
  // 一次性导航提示：运行页引导卡跳来时，校准页打开后预选对应框选模式。
  const pendingCalibMark = shallowRef<'slider' | 'undo' | 'fold' | null>(null)
  // 清除作品时记住的项目 ID：之后重新上传图片仍沿用这份校准（§5 语义）。
  const lastClearedProjectId = shallowRef<string | null>(null)
  // 项目世代号：换图/清除/恢复项目时自增。所有「await 之后写 store」的落点
  // 先比对创建时的世代，不符即丢弃结果，避免慢响应把上一个项目的数据写回
  // 新项目（校准串项目、任务复活等同族竞态的统一防线）。
  const epoch = shallowRef(0)
  // 设备枚举世代：ADB 枚举慢时旧响应不得覆盖新结果。
  let windowsEpoch = 0

  /** 当前项目世代；与 await 前捕获值不一致说明期间已换图/清除/恢复。 */
  function currentEpoch() {
    return epoch.value
  }

  /** 指定项目是否仍是当前项目（跨 await 的轻量身份校验）。 */
  function isCurrentProject(projectId: string | null | undefined) {
    return Boolean(projectId) && project.value?.projectId === projectId
  }

  function requestCalibMark(mark: 'slider' | 'undo' | 'fold') {
    pendingCalibMark.value = mark
  }

  function takeCalibMark(): 'slider' | 'undo' | 'fold' | null {
    const mark = pendingCalibMark.value
    pendingCalibMark.value = null
    return mark
  }

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
    // 换图沿用校准（clear-and-guide-design §5.1）：发起前捕获当前项目 ID，
    // 后端把其最新校准整体复制到新项目，窗口未动时零成本直接可画。
    // 「清除作品」之后 project 已为 null，用清除时记住的 ID 继续沿用。
    const copyFrom = project.value?.projectId ?? lastClearedProjectId.value
    epoch.value += 1
    const myEpoch = epoch.value
    try {
      project.value = await api.createProject(file, name, copyFrom)
      writeLastProjectId(project.value.projectId)
      lastClearedProjectId.value = null
      if (copyFrom) {
        const mine = project.value.projectId
        const state = await api.getResumeState(mine)
        // 期间用户又换了一次图：本次沿用结果属于被顶替的项目，不写。
        if (epoch.value !== myEpoch || !isCurrentProject(mine)) return project.value
        calibration.value = state.calibration
      } else {
        calibration.value = null
      }
      return project.value
    } catch (e) {
      // 已被更新的导入顶替：错误与清空都不属于当前项目，静默上抛即可。
      if (epoch.value !== myEpoch) throw e
      error.value = errorMessage(e)
      calibration.value = null
      throw e
    } finally {
      if (epoch.value === myEpoch) busy.value = false
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
    const myEpoch = ++windowsEpoch
    try {
      const devices = await api.listDevices()
      // 并发枚举时只让最新一次落地：旧响应会把选中句柄回滚成已失效的投屏窗口，
      // 进而对着死句柄触发自动校准。
      if (myEpoch !== windowsEpoch) return
      windows.value = devices.pcWindows
      android.value = devices.android
      mirror.value = devices.mirror
      tools.value = devices.tools
      // 小程序重启后窗口句柄会变：选中的句柄已不在列表里（也不是投屏窗口）
      // 时作废旧选择，回到自动选中，否则校准/绘制会一直对着死句柄报「句柄失效」。
      const selectedAlive =
        selectedHwnd.value !== null &&
        (devices.pcWindows.some((w) => w.hwnd === selectedHwnd.value) ||
          (devices.mirror.running && devices.mirror.hwnd === selectedHwnd.value))
      if (selectedHwnd.value !== null && !selectedAlive) {
        userPicked.value = false
        selectedHwnd.value = null
      }
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
    const projectId = project.value.projectId
    const hwnd = selectedHwnd.value
    try {
      const result = await api.calibrate(projectId, {
        hwnd,
        mode,
        rect: rect ?? undefined,
        deviceKind: deviceKind.value,
        deviceSerial: isMirrorTarget.value ? mirror.value?.serial ?? undefined : undefined,
        slider: extras?.slider,
        undo: extras?.undo,
        fold: extras?.fold,
      })
      // 校准期间换图/清除：结果属于旧窗口帧，写回会让新项目的画布矩形错位。
      if (!isCurrentProject(projectId)) return result
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

  /** 刷新/重开后按 ID 恢复项目与校准（原图走服务端 imageUrl 回读）。 */
  async function restore(target: { projectId: string }, cal: Calibration | null) {
    epoch.value += 1
    project.value = { ...target, imageUrl: `/api/projects/${target.projectId}/image` } as Project
    calibration.value = cal
    writeLastProjectId(target.projectId)
  }

  function reset() {
    epoch.value += 1
    project.value = null
    calibration.value = null
    error.value = null
    pendingCalibMark.value = null
    writeLastProjectId(null)
  }

  /**
   * 清除作品：回到「重新上传图片」的初始状态。
   *
   * 终态任务记录尽量删除（失败不阻塞，本地状态照清）；规划/编辑草稿/参数复位；
   * 记住项目 ID 供之后重新上传时沿用校准。进行中的任务不允许清除（须先停止）。
   */
  async function clearAll(): Promise<boolean> {
    if (busy.value) return false
    const taskStore = useTaskStore()
    const editStore = useEditStore()
    if (taskStore.isActive) {
      error.value = '任务进行中，请先停止再清除'
      return false
    }
    // 任务创建/清空画布/撤销与 AI 编辑都在途时禁止清除：它们的 await 落地会把
    // 已被清除的项目状态写回（任务复活、旧项目还原等假象）。
    if (taskStore.busy || taskStore.clearing || taskStore.undoing) {
      error.value = '有操作正在执行，请等待完成后再清除'
      return false
    }
    if (editStore.busy || editStore.reverting) {
      error.value = 'AI 编辑正在执行，请等待完成后再清除'
      return false
    }
    busy.value = true
    error.value = null
    const myEpoch = epoch.value
    try {
      const snapshot = taskStore.snapshot
      if (snapshot) {
        // 尽力而为：删不掉（如服务重启后记录丢失）也继续清本地。
        await api.deleteTask(snapshot.taskId).catch(() => undefined)
      }
      // 删除请求在途时用户换了图：新项目已建好，不能再按旧意图清空。
      if (epoch.value !== myEpoch) return false
      if (project.value) lastClearedProjectId.value = project.value.projectId
      taskStore.clearLocal()
      usePlanStore().clear()
      useEditStore().clear()
      useParamsStore().reset()
      reset()
      return true
    } finally {
      busy.value = false
    }
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
    pendingCalibMark,
    lastClearedProjectId,
    currentEpoch,
    isCurrentProject,
    requestCalibMark,
    takeCalibMark,
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
    restore,
    reset,
    clearAll,
  }
})
