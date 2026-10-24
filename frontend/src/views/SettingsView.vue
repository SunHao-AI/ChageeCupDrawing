<script setup lang="ts">
import { computed, onMounted, shallowRef } from 'vue'
import ErrorBanner from '@/components/ErrorBanner.vue'
import { useAiStore } from '@/stores/ai'
import { clearAiKey, maskAiKey, readAiKey, writeAiKey } from '@/utils/aiKey'
import {
  AI_CATALOG,
  CATALOG_GROUPS,
  STATUS_TEXT,
  matchCatalog,
  modelOptions,
  type AiCatalogEntry,
} from '@/data/aiCatalog'
import type { ViewEmits } from './types'

const emit = defineEmits<ViewEmits>()
const aiStore = useAiStore()

const provider = shallowRef('')
const model = shallowRef('')
const baseUrl = shallowRef('')
const apiKey = shallowRef('')
// Key 只在本机浏览器：这里展示的是 localStorage 中已存的掩码，不是服务端回传。
const localKeyMask = shallowRef('')
const clearKey = shallowRef(false)
const allowCloud = shallowRef(false)
const saved = shallowRef(false)
const catalogId = shallowRef('')

const picked = computed<AiCatalogEntry | null>(
  () => AI_CATALOG.find((e) => e.id === catalogId.value) ?? null,
)
const models = computed(() => modelOptions(provider.value, picked.value))

const knownProviders = computed(() => {
  const ids = new Map<string, string>()
  for (const p of aiStore.generativeProviders) ids.set(p.id, p.label)
  const savedId = aiStore.settings?.provider
  if (savedId && !ids.has(savedId)) ids.set(savedId, `${savedId}（已保存）`)
  return [...ids.entries()].map(([id, label]) => ({ id, label }))
})

// 勾选后仍要显示复选框本身，否则它会随 clearKey 消失、再也无法取消。
const hasStoredKey = computed(() => !!localKeyMask.value || aiStore.settings?.hasKey === true)
const envLocked = computed(() => aiStore.settings?.envOverridesKey === true)

onMounted(async () => {
  await aiStore.load()
  const s = aiStore.settings
  localKeyMask.value = maskAiKey(readAiKey())
  if (!s) return
  provider.value = s.provider ?? ''
  model.value = s.model ?? ''
  baseUrl.value = s.baseUrl ?? ''
  allowCloud.value = s.allowCloudEdit
  catalogId.value = matchCatalog(s.provider ?? '', s.model ?? '', s.baseUrl ?? '')?.id ?? ''
  probe()
})

/** 预检直接用表单当前值，不必先保存；Key 由请求头携带。 */
function probePayload() {
  return {
    provider: provider.value.trim(),
    model: model.value.trim() || undefined,
    baseUrl: baseUrl.value.trim(),
    allowCloudEdit: allowCloud.value,
  }
}

function probe() {
  void aiStore.test(probePayload())
}

function onCatalogChange(event: Event) {
  saved.value = false
  // 原生 select 的 v-model 监听在指令 created 阶段才挂上，晚于模板 @change，
  // 直接读事件值可避免拿到上一次的 catalogId。
  const id = (event.target as HTMLSelectElement).value
  catalogId.value = id
  const entry = AI_CATALOG.find((e) => e.id === id)
  if (!entry) return
  // 所有条目都回填，避免留上一家的地址；未接入项由详情卡明确提示不会生效。
  provider.value = entry.provider ?? ''
  model.value = entry.model ?? entry.models?.[0] ?? ''
  baseUrl.value = entry.baseUrl ?? ''
  probe()
}

function onProviderInput(event: Event) {
  provider.value = (event.target as HTMLInputElement).value
  catalogId.value = ''
  saved.value = false
}

function save() {
  saved.value = false
  // Key 只写浏览器本地；服务端请求体不再带 Key。
  if (clearKey.value) {
    clearAiKey()
    localKeyMask.value = ''
  } else if (apiKey.value.trim()) {
    writeAiKey(apiKey.value)
    localKeyMask.value = maskAiKey(readAiKey())
  }
  aiStore.refreshKey()
  const keepLocalKey = !clearKey.value
  aiStore
    .save({
      provider: provider.value.trim(),
      model: model.value.trim() || undefined,
      baseUrl: baseUrl.value.trim(),
      allowCloudEdit: allowCloud.value,
    })
    .then((ok) => {
      if (ok) {
        saved.value = true
        apiKey.value = ''
        clearKey.value = false
        if (!keepLocalKey) localKeyMask.value = ''
      }
    })
}

</script>

<template>
  <div class="settings">
    <section class="card panel">
      <h3 class="card-title">AI 编辑（自带 API Key）</h3>
      <p class="desc">
        固定变换（上移/缩放/加粗等）与已支持的口语指令始终在本地执行、零成本；
        只有规则无法理解的创作类指令（加艺术字、删边框、改画图等）才会发送到你配置的云端模型。
        API Key 只保存在本机浏览器，仅在编辑与预检时随请求单次携带，服务端不落盘、不回显。
      </p>
      <ErrorBanner v-if="aiStore.error" kind="error">{{ aiStore.error }}</ErrorBanner>

      <label class="field">
        <span class="label">服务 / 模型</span>
        <select
          v-model="catalogId"
          class="input"
          :disabled="aiStore.busy"
          @change="onCatalogChange"
        >
          <option value="">未选择（沿用已保存或下方手动配置）</option>
          <optgroup v-for="group in CATALOG_GROUPS" :key="group.status" :label="group.label">
            <option v-for="entry in group.entries" :key="entry.id" :value="entry.id">
              {{ entry.name }} · {{ entry.price }}
            </option>
          </optgroup>
        </select>
        <span class="hint">
          国内外常用模型的价格为 2026-10 调研近似值，以官网现价为准；任意一项都会自动填好模型与服务地址。
        </span>
      </label>

      <div v-if="picked" class="catalog">
        <div class="catalog-head">
          <strong>{{ picked.name }}</strong>
          <span class="tag" :class="`tag-${picked.status}`">{{ STATUS_TEXT[picked.status] }}</span>
          <span class="tag">{{ picked.region }}</span>
          <span class="tag" :class="picked.price === '免费' ? 'tag-free' : 'tag-paid'">
            {{ picked.price }}
          </span>
        </div>
        <p class="catalog-price">{{ picked.priceNote }}</p>
        <p v-if="picked.note" class="catalog-note">{{ picked.note }}</p>
        <p v-if="picked.status !== 'ready'" class="catalog-warn">
          已自动填好接口地址与模型。该接口尚未接入生成通道：保存会替换当前配置且暂不能出图，现在要改图请选「可直接使用」的服务。
        </p>
        <div class="steps">
          <span class="steps-title">Key 获取 / 接入步骤</span>
          <ol>
            <li v-for="step in picked.steps" :key="step">{{ step }}</li>
          </ol>
        </div>
        <a
          v-if="picked.keyUrl"
          class="catalog-link"
          :href="picked.keyUrl"
          target="_blank"
          rel="noreferrer"
        >
          {{ picked.keyLabel ?? picked.keyUrl }} ↗
          <span class="catalog-link-url">{{ picked.keyUrl }}</span>
        </a>
      </div>

      <label class="field">
        <span class="label">模型</span>
        <input
          v-model="model"
          class="input"
          placeholder="留空使用默认模型（gemini-3.1-flash-image / gpt-image-1.5）"
          :disabled="aiStore.busy"
          @input="saved = false"
        />
        <div v-if="models.length" class="model-choices">
          <button
            v-for="name in models"
            :key="name"
            type="button"
            class="chip"
            :class="{ active: model === name }"
            :disabled="aiStore.busy"
            @click="model = name; saved = false; probe()"
          >
            {{ name }}
          </button>
        </div>
      </label>

      <label class="field">
        <span class="label">服务地址（OpenAI 兼容 / 本地部署）</span>
        <input
          v-model="baseUrl"
          class="input"
          placeholder="如 http://127.0.0.1:11434/v1，留空表示不启用该通道"
          :disabled="aiStore.busy"
          @input="saved = false"
        />
        <span class="hint">
          指向本机时不出网、免 Key、也不受下面的云上传授权限制；指向远端则按云端处理。
        </span>
      </label>

      <details class="advanced">
        <summary>高级：手动填写 provider</summary>
        <label class="field">
          <span class="label">Provider</span>
          <input
            v-model="provider"
            class="input"
            list="ai-providers"
            placeholder="gemini-image 或 openai-compatible"
            :disabled="aiStore.busy"
            @input="onProviderInput"
          />
          <datalist id="ai-providers">
            <option v-for="p in knownProviders" :key="p.id" :value="p.id">{{ p.label }}</option>
          </datalist>
          <span class="hint">通常由上方目录自动填入，仅在接入自定义网关时需要手改。</span>
        </label>
      </details>

      <label class="field">
        <span class="label">API Key（仅存本机浏览器，不上传服务器）</span>
        <template v-if="envLocked">
          <p class="env-note">已由环境变量注入（{{ aiStore.settings?.apiKeyMasked }}），接口不可修改。</p>
        </template>
        <template v-else>
          <input
            v-model="apiKey"
            class="input"
            type="password"
            autocomplete="off"
            :placeholder="localKeyMask || '填写后保存到浏览器；不会发送到服务器'"
            :disabled="aiStore.busy || clearKey"
            @input="saved = false"
          />
          <label v-if="hasStoredKey" class="check inline">
            <input v-model="clearKey" type="checkbox" />
            清除本机已保存的 Key（{{ localKeyMask }}）
          </label>
        </template>
      </label>

      <label class="check">
        <input v-model="allowCloud" type="checkbox" :disabled="aiStore.busy" @change="saved = false" />
        允许将选区图片上传到所选第三方服务进行处理（含计费）
      </label>

      <div class="actions">
        <button class="btn btn-primary btn-sm" :disabled="aiStore.busy" @click="save">
          {{ aiStore.busy ? '处理中…' : '保存' }}
        </button>
        <button class="btn btn-sm" :disabled="aiStore.busy || aiStore.testing" @click="probe">
          {{ aiStore.testing ? '预检中…' : '配置预检' }}
        </button>
        <span v-if="saved" class="saved num">已保存</span>
      </div>
      <p class="hint">选择服务或切换模型后会自动预检当前表单（只查模型列表，不计费），无需先保存。</p>

      <div v-if="aiStore.testResult" class="banner" :class="aiStore.testResult.ok ? 'banner-info' : 'banner-warn'">
        {{ aiStore.testResult.text }}
      </div>

      <div class="status" :class="{ ready: aiStore.generativeReady }">
        生成通道：{{ aiStore.generativeReady ? '已就绪' : '未就绪' }}
        <span class="status-detail">
          规则能理解的指令始终在本地执行、零成本；只有创作类指令才走所选模型。
          宽画布会先按选区或内容区裁块再交给模型，结果羽化回填后仍需你确认才会生效。
        </span>
      </div>

      <div class="actions">
        <button class="btn btn-sm" @click="emit('goto', 'studio')">返回工作台</button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.settings {
  max-width: 640px;
}

.panel {
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.desc {
  margin: 0;
  font-size: 12px;
  color: var(--text-dim);
  line-height: 1.6;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.label {
  font-size: 12px;
  color: var(--text-faint);
}

.input {
  background: var(--ink-0);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  color: var(--text);
  padding: 7px 10px;
  font-size: 13px;
}

.input:focus {
  outline: none;
  border-color: var(--gold);
}

/* scoped 特异性高于全局触摸规则，单独保证 iOS 聚焦不放大 */
@media (pointer: coarse), (max-width: 1376px) {
  .input {
    font-size: 16px;
  }

  .desc,
  .hint,
  .chip,
  .env-note,
  .steps-title,
  .catalog-link-url {
    font-size: 13px;
  }

  .tag {
    font-size: 12px;
  }
}

.env-note {
  margin: 0;
  font-size: 12px;
  color: var(--text-dim);
}

.hint {
  font-size: 11px;
  color: var(--text-faint);
  line-height: 1.5;
}

.model-choices {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 2px;
}

.chip {
  padding: 3px 9px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--ink-2);
  color: var(--text-dim);
  font-size: 11px;
  cursor: pointer;
}

.chip:hover {
  border-color: var(--gold-soft);
  color: var(--text);
}

.chip.active {
  border-color: var(--gold);
  background: var(--gold-dim);
  color: var(--gold);
}

.chip:disabled {
  opacity: 0.6;
  cursor: default;
}

.check {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  font-size: 12px;
  color: var(--text-dim);
  line-height: 1.5;
  cursor: pointer;
}

.check.inline {
  align-items: center;
}

.actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.saved {
  color: var(--green, #7bc47f);
  font-size: 12px;
}

.catalog {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--ink-1);
}

.catalog-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}

.tag {
  padding: 1px 6px;
  border: 1px solid var(--line);
  border-radius: 999px;
  font-size: 10px;
  color: var(--text-dim);
}

.tag-ready {
  border-color: var(--gold-soft);
  background: var(--gold-dim);
  color: var(--gold);
}

.tag-native {
  border-color: var(--amber);
  color: var(--amber);
}

.tag-text {
  border-color: var(--red);
  color: var(--red);
}

.tag-free {
  border-color: var(--green);
  color: var(--green);
}

.tag-paid {
  color: var(--text-faint);
}

.catalog-price {
  margin: 0;
  font-size: 12px;
  color: var(--gold);
}

.catalog-note,
.catalog-warn {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-dim);
}

.catalog-warn {
  color: var(--amber);
}

.steps {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-top: 4px;
  border-top: 1px dashed var(--line-soft);
}

.steps-title {
  font-size: 11px;
  color: var(--text-faint);
}

.steps ol {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-dim);
}

.catalog-link {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 12px;
  color: var(--gold);
  text-decoration: none;
}

.catalog-link:hover {
  text-decoration: underline;
}

.catalog-link-url {
  font-size: 11px;
  color: var(--text-faint);
  word-break: break-all;
}

.advanced summary {
  font-size: 12px;
  color: var(--text-faint);
  cursor: pointer;
}

.advanced .field {
  margin-top: 6px;
}

.status {
  font-size: 12px;
  color: var(--text-dim);
  border: 1px dashed var(--line);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
}

.status.ready {
  border-color: var(--gold-soft);
}

.status-detail {
  display: block;
  margin-top: 4px;
  color: var(--text-faint);
}
</style>
