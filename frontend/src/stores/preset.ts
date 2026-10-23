import { defineStore } from 'pinia'
import { shallowRef } from 'vue'
import * as api from '@/api/endpoints'
import type { Preset } from '@/api/types'
import { errorMessage } from '@/utils/error'
import { withPlanDefaults } from '@/params/registry'

export const usePresetStore = defineStore('preset', () => {
  const items = shallowRef<Preset[]>([])
  const error = shallowRef<string | null>(null)
  const busy = shallowRef(false)

  async function load() {
    error.value = null
    try {
      items.value = (await api.listPresets()).map((p) => ({ ...p, params: withPlanDefaults(p.params) }))
    } catch (e) {
      error.value = errorMessage(e)
    }
  }

  async function save(name: string, params: Preset['params']) {
    busy.value = true
    error.value = null
    try {
      const created = await api.createPreset(name, params)
      items.value = [...items.value, { ...created, params: withPlanDefaults(created.params) }]
      return created
    } catch (e) {
      error.value = errorMessage(e)
      throw e
    } finally {
      busy.value = false
    }
  }

  return { items, error, busy, load, save }
})
