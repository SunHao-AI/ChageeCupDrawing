<script setup lang="ts">
import { computed } from 'vue'
import type { FieldMeta } from '@/params/registry'

const props = defineProps<{
  meta: FieldMeta
  modelValue: string | number | boolean
  disabled?: boolean
  /** 条件字段的可见性由父层计算后传入。 */
}>()

const emit = defineEmits<{ 'update:modelValue': [value: string | number | boolean] }>()

const numeric = computed(() => Number(props.modelValue))
const text = computed(() => String(props.modelValue))

function onSlider(event: Event) {
  const raw = (event.target as HTMLInputElement).valueAsNumber
  const meta = props.meta
  if (meta.kind !== 'slider') return
  emit('update:modelValue', meta.precision === 0 ? Math.round(raw) : raw)
}
</script>

<template>
  <div v-if="meta.kind === 'slider'" class="field">
    <div class="field-label">
      <span>{{ meta.label }}</span>
      <span class="field-value num">{{ numeric.toFixed(meta.precision) }}</span>
    </div>
    <input
      type="range"
      :min="meta.min"
      :max="meta.max"
      :step="meta.step"
      :value="numeric"
      :disabled="disabled"
      @input="onSlider"
    />
    <div v-if="meta.hint" class="hint">{{ meta.hint }}</div>
  </div>

  <div v-else-if="meta.kind === 'segmented'" class="field">
    <div class="field-label"><span>{{ meta.label }}</span></div>
    <div class="segmented">
      <button
        v-for="option in meta.options"
        :key="option.value"
        :class="{ active: text === option.value }"
        :disabled="disabled"
        @click="emit('update:modelValue', option.value)"
      >
        {{ option.label }}
      </button>
    </div>
  </div>

  <div v-else class="field toggle-row">
    <label class="switch">
      <input type="checkbox" :checked="Boolean(modelValue)" :disabled="disabled" @change="emit('update:modelValue', ($event.target as HTMLInputElement).checked)" />
      <span>{{ meta.label }}</span>
    </label>
    <div v-if="meta.hint" class="hint">{{ meta.hint }}</div>
  </div>
</template>

<style scoped>
.hint {
  font-size: 11px;
  color: var(--text-faint);
  margin-top: 3px;
}

@media (pointer: coarse), (max-width: 1376px) {
  .hint {
    font-size: 12px;
  }
}

.toggle-row {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
</style>
