/** 「清除作品」（clearAll）与换图沿用校准的前端状态机回归。 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import * as api from '@/api/endpoints'
import type { Calibration, Project, TaskSnapshot } from '@/api/types'
import { defaultPlanParams } from '@/params/registry'
import { useEditStore } from './edit'
import { useParamsStore } from './params'
import { usePlanStore } from './plan'
import { useProjectStore } from './project'
import { useTaskStore } from './task'

vi.mock('@/api/endpoints', async (importActual) => ({
  ...(await importActual<typeof import('@/api/endpoints')>()),
  deleteTask: vi.fn(),
  createProject: vi.fn(),
  getResumeState: vi.fn(),
}))

// 会话记忆的最小替身：clearAll 会把「上次项目」记忆清掉。
const storage = new Map<string, string>()
vi.stubGlobal('window', {
  localStorage: {
    getItem: (k: string) => storage.get(k) ?? null,
    setItem: (k: string, v: string) => storage.set(k, v),
    removeItem: (k: string) => storage.delete(k),
  },
})

function fakeProject(id: string): Project {
  return { projectId: id, name: `${id}.png`, imageUrl: '/x', width: 10, height: 10 } as Project
}

function fakeCalibration(): Calibration {
  return { rect: { x: 1, y: 2, w: 300, h: 400 } } as Calibration
}

function doneSnapshot(taskId = 'task-1'): TaskSnapshot {
  return {
    taskId,
    planId: 'plan-1',
    status: 'DONE',
    cursorPath: 40,
    totalPaths: 40,
    exec: {},
    stopReason: null,
    errorCode: null,
    startedAt: '',
    finishedAt: '',
  } as TaskSnapshot
}

beforeEach(() => {
  setActivePinia(createPinia())
  storage.clear()
  vi.mocked(api.deleteTask).mockReset().mockResolvedValue(undefined)
  vi.mocked(api.createProject).mockReset()
  vi.mocked(api.getResumeState).mockReset()
})

describe('clearAll（清除作品，回到重新上传图片的初始状态）', () => {
  it('终态任务：删后端记录并清空全部本地状态', async () => {
    const projectStore = useProjectStore()
    const taskStore = useTaskStore()
    const planStore = usePlanStore()
    const paramsStore = useParamsStore()
    const editStore = useEditStore()

    await projectStore.restore(fakeProject('p1'), fakeCalibration())
    taskStore.snapshot = doneSnapshot()
    planStore.summary = { strategy: 'scan' } as never
    paramsStore.setPlan({ imageMode: 'adaptive' })
    editStore.error = 'stale'
    storage.set('cupdraw.lastProject', 'p1')

    await expect(projectStore.clearAll()).resolves.toBe(true)

    expect(api.deleteTask).toHaveBeenCalledWith('task-1')
    expect(projectStore.project).toBeNull()
    expect(projectStore.calibration).toBeNull()
    expect(taskStore.snapshot).toBeNull()
    expect(planStore.summary).toBeNull()
    expect(editStore.error).toBeNull()
    expect(paramsStore.plan.imageMode).toBe(defaultPlanParams().imageMode)
    expect(storage.has('cupdraw.lastProject')).toBe(false)
    // 校准虽已清空，但项目 ID 被记住：重新上传时走 copyCalibrationFrom 免校准。
    expect(projectStore.lastClearedProjectId).toBe('p1')
  })

  it('任务进行中拒绝清除，状态原封不动', async () => {
    const projectStore = useProjectStore()
    const taskStore = useTaskStore()
    await projectStore.restore(fakeProject('p1'), fakeCalibration())
    taskStore.snapshot = { ...doneSnapshot(), status: 'DRAWING' } as TaskSnapshot

    await expect(projectStore.clearAll()).resolves.toBe(false)

    expect(api.deleteTask).not.toHaveBeenCalled()
    expect(projectStore.project).not.toBeNull()
    expect(taskStore.snapshot).not.toBeNull()
    expect(projectStore.error).toContain('请先停止')
  })

  it('后端删除失败不阻塞本地清除（尽力而为）', async () => {
    const projectStore = useProjectStore()
    const taskStore = useTaskStore()
    vi.mocked(api.deleteTask).mockRejectedValue(new Error('gone'))
    await projectStore.restore(fakeProject('p1'), fakeCalibration())
    taskStore.snapshot = doneSnapshot()

    await expect(projectStore.clearAll()).resolves.toBe(true)

    expect(projectStore.project).toBeNull()
    expect(taskStore.snapshot).toBeNull()
    expect(projectStore.lastClearedProjectId).toBe('p1')
  })

  it('无任务快照也能清除（只清项目与规划）', async () => {
    const projectStore = useProjectStore()
    await projectStore.restore(fakeProject('p1'), fakeCalibration())

    await expect(projectStore.clearAll()).resolves.toBe(true)

    expect(api.deleteTask).not.toHaveBeenCalled()
    expect(projectStore.project).toBeNull()
  })
})

describe('清除后重新上传图片沿用校准', () => {
  it('createProject 携带 copyCalibrationFrom=被清除的项目并回读校准', async () => {
    const projectStore = useProjectStore()
    const taskStore = useTaskStore()
    await projectStore.restore(fakeProject('p1'), fakeCalibration())
    taskStore.snapshot = doneSnapshot()
    await projectStore.clearAll()

    const cal = fakeCalibration()
    vi.mocked(api.createProject).mockResolvedValue(fakeProject('p2'))
    vi.mocked(api.getResumeState).mockResolvedValue({ calibration: cal } as never)

    const file = { name: 'new.png' } as File
    await projectStore.createProject(file)

    expect(api.createProject).toHaveBeenCalledWith(file, undefined, 'p1')
    expect(projectStore.calibration).toEqual(cal)
    expect(projectStore.lastClearedProjectId).toBeNull()
  })

  it('未清除过（无当前项目也无记忆 ID）则不沿用', async () => {
    const projectStore = useProjectStore()
    vi.mocked(api.createProject).mockResolvedValue(fakeProject('p2'))

    await projectStore.createProject({ name: 'a.png' } as File)

    expect(api.createProject).toHaveBeenCalledWith(expect.anything(), undefined, null)
    expect(projectStore.calibration).toBeNull()
    expect(api.getResumeState).not.toHaveBeenCalled()
  })
})
