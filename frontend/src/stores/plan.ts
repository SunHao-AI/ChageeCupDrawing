import { defineStore } from 'pinia'
import { shallowRef } from 'vue'
import * as api from '@/api/endpoints'
import type { PlanSummary } from '@/api/types'
import { errorMessage } from '@/utils/error'
import { useParamsStore } from './params'
import { useProjectStore } from './project'

const DEBOUNCE_MS = 300

export const usePlanStore = defineStore('plan', () => {
  const summary = shallowRef<PlanSummary | null>(null)
  /** 规划在途：保留上一版预览并标灰。 */
  const pending = shallowRef(false)
  const error = shallowRef<string | null>(null)

  let timer: ReturnType<typeof setTimeout> | null = null
  // 最近一次成功规划的参数签名：同参数重复触发时跳过，避免空耗规划进程池。
  let lastSignature: string | null = null
  // 单飞 + 结束后合并补跑：后端规划池只有一个 worker，中止在途请求既不真正
  // 取消计算，又会在控制台留下 ERR_ABORTED 噪声；改为在途期间只记一次
  // 「稍后用最新参数补跑」，请求串行且不叠加。
  let running = false
  let rerunRequested = false
  let rerunForce = false
  let generation = 0

  function cancel() {
    if (timer) clearTimeout(timer)
    timer = null
    pending.value = false
  }

  function clear() {
    cancel()
    generation += 1
    rerunRequested = false
    rerunForce = false
    summary.value = null
    error.value = null
    lastSignature = null
  }

  /** 参数改动后去抖触发；拖动过程中不排队全量规划。 */
  function schedule() {
    if (timer) clearTimeout(timer)
    timer = setTimeout(run, DEBOUNCE_MS)
  }

  /** force 用于源图本身变化（如应用编辑草稿）后的重规划，此时参数签名并未改变。 */
  function run(force = false) {
    timer = null
    const projectStore = useProjectStore()
    if (!projectStore.project || !projectStore.calibrated) return
    if (running) {
      rerunRequested = true
      rerunForce = rerunForce || force
      return
    }
    void exec(force)
  }

  async function exec(force: boolean) {
    running = true
    pending.value = true
    error.value = null
    const gen = generation
    try {
      let nextForce = force
      for (;;) {
        const paramsStore = useParamsStore()
        const signature = JSON.stringify(paramsStore.plan)
        // 同参数已完成过规划就不再重跑：watch 可能因对象引用变化误触发，
        // 而一次规划要占用单进程规划池十几秒，必须挡住空转。
        if (!nextForce && summary.value && signature === lastSignature) break
        try {
          const result = await api.createPlan(
            useProjectStore().project!.projectId,
            { ...paramsStore.plan },
          )
          if (gen !== generation) break
          summary.value = result
          lastSignature = signature
          paramsStore.markPlanned()
        } catch (e) {
          if (gen !== generation) break
          error.value = errorMessage(e)
          break
        }
        if (!rerunRequested) break
        nextForce = rerunForce
        rerunRequested = false
        rerunForce = false
      }
    } finally {
      rerunRequested = false
      rerunForce = false
      if (gen === generation) pending.value = false
      running = false
    }
  }

  return { summary, pending, error, schedule, run, cancel, clear }
})
