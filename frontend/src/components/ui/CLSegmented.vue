<script setup lang="ts">
defineProps<{ modelValue: string; options: { label: string; value: string }[] }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: string): void }>()
</script>

<template>
  <div class="clseg">
    <button
      v-for="o in options"
      :key="o.value"
      :class="['clseg-item', { on: modelValue === o.value }]"
      @click="emit('update:modelValue', o.value)"
    >
      {{ o.label }}
    </button>
  </div>
</template>

<style scoped>
/* 深色玻璃胶囊。浅色板的做法是"凹槽用灰底、选中项用白底+阴影浮起来"；
   深色下这套反过来了 —— 凹槽压暗，选中项靠品牌渐变**发光**浮起，
   照搬白底会在深色页面上留一块刺眼的亮片。 */
.clseg {
  display: flex; padding: 4px; gap: 4px;
  /* 选项多的时候（助教工作台五个页签）手机宽度放不下：在胶囊内部横滑，
     不把整页撑出横向滚动条 */
  max-width: 100%; overflow-x: auto; scrollbar-width: none;
  background: var(--cl-inset);
  border: 1px solid var(--cl-glass-border);
  border-radius: var(--cl-radius-sm);
}
.clseg::-webkit-scrollbar { display: none; }
.clseg-item {
  flex: 1; padding: 9px 14px; border: none; cursor: pointer; font: inherit; font-weight: 600;
  color: var(--cl-text-2); background: transparent; border-radius: calc(var(--cl-radius-sm) - 3px);
  white-space: nowrap;
  transition: color .15s ease, background .15s ease, box-shadow .15s ease;
}
.clseg-item:hover { color: var(--cl-text); background: var(--cl-tint); }
.clseg-item:focus-visible { outline: 2px solid var(--cl-brand); outline-offset: -2px; }
.clseg-item.on {
  color: var(--cl-on-brand); background: var(--cl-brand-grad);
  box-shadow: var(--cl-glow);
}
.clseg-item.on:hover { color: var(--cl-on-brand); background: var(--cl-brand-grad); }
</style>
