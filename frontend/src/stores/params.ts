import { defineStore } from 'pinia'
import { reactive, shallowRef } from 'vue'
import type { ExecParams, PlanParams } from '@/api/types'
import {
  defaultExecParams,
  defaultPlanParams,
  withExecDefaults,
  withPlanDefaults,
} from '@/params/registry'

export const useParamsStore = defineStore('params', () => {
  const plan = reactive<PlanParams>(defaultPlanParams())
  const exec = reactive<ExecParams>(defaultExecParams())
  /** plan 改动后、规划成功前为 true。 */
  const dirty = shallowRef(false)

  function setPlan(partial: Partial<PlanParams>) {
    Object.assign(plan, partial)
    dirty.value = true
  }

  function replacePlan(next: PlanParams) {
    Object.assign(plan, withPlanDefaults(next))
    dirty.value = true
  }

  function setExec(partial: Partial<ExecParams>) {
    Object.assign(exec, partial)
  }

  function replaceExec(next: ExecParams) {
    Object.assign(exec, withExecDefaults(next))
  }

  function markPlanned() {
    dirty.value = false
  }

  function reset() {
    Object.assign(plan, defaultPlanParams())
    Object.assign(exec, defaultExecParams())
    dirty.value = false
  }

  return { plan, exec, dirty, setPlan, replacePlan, setExec, replaceExec, markPlanned, reset }
})
