import { defineStore } from 'pinia'
import { computed, shallowRef } from 'vue'
import * as api from '@/api/endpoints'
import type { AiProbeRequest, AiSettingsInfo, EditProviderInfo } from '@/api/types'
import { errorMessage } from '@/utils/error'
import { readAiKey } from '@/utils/aiKey'

/** AI 编辑设置状态：配置读写、预检、provider 目录（Key 只回掩码）。 */
export const useAiStore = defineStore('ai', () => {
  const settings = shallowRef<AiSettingsInfo | null>(null)
  const providers = shallowRef<EditProviderInfo[]>([])
  const busy = shallowRef(false)
  // 预检独立于保存：预检可能耗时数十秒，不能把整个表单锁住让用户无法填 Key。
  const testing = shallowRef(false)
  const error = shallowRef<string | null>(null)
  const testResult = shallowRef<{ ok: boolean; text: string; reason?: string } | null>(null)
  // 浏览器本地 Key 的响应式镜像：localStorage 本身不可监听，改动后调用 refreshKey。
  const localKey = shallowRef(readAiKey())

  const generativeProviders = computed(() =>
    providers.value.filter((p) => p.kind === 'generative' || p.kind === 'both'),
  )
  const selectedProvider = computed(
    () => providers.value.find((p) => p.id === settings.value?.provider) ?? null,
  )
  /** 是否有可用 Key：Key 只存浏览器，服务端 hasKey 仅代表环境变量或旧配置。 */
  const hasKey = computed(() => !!localKey.value || settings.value?.hasKey === true)
  /** 生成通道是否可用：本地部署免 Key 免授权；云端需有 Key 且已授权上传。 */
  const generativeReady = computed(() => {
    const p = selectedProvider.value
    if (!p || !settings.value?.provider) return false
    if (!p.cloud) return true
    return hasKey.value && settings.value.allowCloudEdit
  })

  async function load() {
    localKey.value = readAiKey()
    try {
      const [s, ps] = await Promise.all([api.getAiSettings(), api.editProviders()])
      settings.value = s
      providers.value = ps
    } catch (e) {
      error.value = errorMessage(e)
    }
  }

  async function save(payload: Parameters<typeof api.putAiSettings>[0]) {
    busy.value = true
    error.value = null
    try {
      settings.value = await api.putAiSettings(payload)
      return true
    } catch (e) {
      error.value = errorMessage(e)
      return false
    } finally {
      busy.value = false
    }
  }

  async function test(payload?: AiProbeRequest) {
    testing.value = true
    testResult.value = null
    try {
      const result = await api.testAiSettings(payload)
      testResult.value = {
        ok: result.ok,
        reason: result.reason,
        text: result.ok
          ? `配置可用（provider: ${result.provider}）。`
          : `预检未通过：${REASONS[result.reason ?? 'unknown'] ?? result.reason}。`,
      }
    } catch (e) {
      testResult.value = { ok: false, text: errorMessage(e) }
    } finally {
      testing.value = false
    }
  }

  return {
    settings,
    providers,
    generativeProviders,
    selectedProvider,
    hasKey,
    generativeReady,
    busy,
    testing,
    error,
    testResult,
    refreshKey: () => {
      localKey.value = readAiKey()
    },
    load,
    save,
    test,
  }
})

const REASONS: Record<string, string> = {
  missing_key: '尚未填写 API Key',
  missing_provider: '尚未选择 provider',
  no_provider: '没有已注册的生成 provider',
  provider_not_integrated: '所选服务尚未接入生成通道，请改用「可直接使用」中的服务',
  consent_required: '尚未授权图片上传到云端',
  auth: 'API Key 无效或额度不足（服务端已拒绝）',
  timeout: '服务响应超时，请检查网络或服务地址',
  unreachable: '无法连接服务，请检查地址与网络',
}
