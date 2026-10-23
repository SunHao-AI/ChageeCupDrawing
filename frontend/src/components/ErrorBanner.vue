<script setup lang="ts">
defineProps<{ kind: 'error' | 'warn' | 'info' }>()
</script>

<template>
  <div class="banner" :class="`banner-${kind}`">
    <span class="dot"></span>
    <div class="body"><slot /></div>
  </div>
</template>

<style scoped>
.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  margin-top: 6px;
  flex: none;
}

.body {
  flex: 1;
}
</style>
