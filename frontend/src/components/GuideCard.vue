<script setup lang="ts">
/** 就地引导卡：一句话「现在该做什么」+ 直达操作；额外说明走默认插槽。 */
defineProps<{
  badge?: string
  title: string
  action?: string
  tone?: 'gold' | 'urgent'
}>()

defineEmits<{ action: [] }>()
</script>

<template>
  <div class="guide" :class="tone === 'urgent' ? 'urgent' : ''">
    <div class="main">
      <span v-if="badge" class="badge num">{{ badge }}</span>
      <div class="text">
        <div class="title">{{ title }}</div>
        <div v-if="$slots.default" class="body"><slot /></div>
      </div>
    </div>
    <button v-if="action" class="btn btn-primary btn-sm" @click="$emit('action')">{{ action }}</button>
  </div>
</template>

<style scoped>
.guide {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border: 1px solid var(--gold-soft);
  background: var(--gold-dim);
  border-radius: var(--radius-sm);
}

.guide.urgent {
  border-color: rgba(217, 164, 65, 0.55);
  background: rgba(217, 164, 65, 0.14);
  animation: pulse 1.1s ease-in-out infinite;
}

@keyframes pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(216, 169, 78, 0); }
  50% { box-shadow: 0 0 0 4px rgba(216, 169, 78, 0.18); }
}

.main {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  min-width: 0;
}

.badge {
  flex: none;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--gold);
  color: #1a1408;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
}

.title {
  font-size: 13px;
  color: var(--text);
  font-weight: 600;
}

.body {
  font-size: 12px;
  color: var(--text-dim);
  margin-top: 2px;
}
</style>
