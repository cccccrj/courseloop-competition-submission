<script setup lang="ts">
defineProps<{ title?: string; hint?: string }>()
</script>

<template>
  <div class="empty">
    <svg class="art" viewBox="0 0 120 90" aria-hidden="true">
      <g class="edges"><path d="M60 20 L34 48 M60 20 L86 48 M34 48 L22 72 M34 48 L46 72 M86 48 L74 72 M86 48 L98 72" /></g>
      <g class="nodes">
        <circle cx="60" cy="20" r="6" /><circle cx="34" cy="48" r="5" /><circle cx="86" cy="48" r="5" />
        <circle cx="22" cy="72" r="4" /><circle cx="46" cy="72" r="4" /><circle cx="74" cy="72" r="4" /><circle cx="98" cy="72" r="4" />
      </g>
    </svg>
    <div class="title">{{ title || '暂无内容' }}</div>
    <div v-if="hint" class="hint">{{ hint }}</div>
    <div v-if="$slots.default" class="action"><slot /></div>
  </div>
</template>

<style scoped>
.empty { text-align: center; padding: 40px 20px; color: var(--cl-text-2); }
/* 树形插画：深色下 opacity:.55 会把整张图压进背景里看不见，改成不降整体透明度，
   靠边线和节点各自的色值分层。节点给一点辉光，呼应全站的"科技感光点"。 */
.art { width: 108px; height: 82px; }
.art .edges path { stroke: var(--cl-tint-border); stroke-width: 2; fill: none; }
.art .nodes circle { fill: var(--cl-brand); opacity: .75; }
/* 根节点单独提亮，让这张图有个视觉落点，而不是七个一样的点 */
.art .nodes circle:first-child { fill: var(--cl-accent); opacity: .9; }
.title { font-weight: 700; font-size: 15px; color: var(--cl-text); margin-top: 6px; }
.hint { font-size: 13.5px; color: var(--cl-text-3); margin-top: 4px; }
.action { margin-top: 16px; }
</style>
