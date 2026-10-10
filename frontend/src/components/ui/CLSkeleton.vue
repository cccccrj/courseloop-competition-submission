<script setup lang="ts">
withDefaults(defineProps<{ rows?: number; card?: boolean }>(), { rows: 3, card: true })
</script>

<template>
  <div class="sk" :class="{ card }">
    <div v-for="i in rows" :key="i" class="line" :style="{ width: (60 + (i * 37) % 40) + '%' }"></div>
  </div>
</template>

<style scoped>
/* 骨架屏必须和它将要变成的那张卡长得一样，否则加载完成时会"跳"一下。
   因此这里跟 CLCard 用同一套玻璃取值，包括顶缘高光。 */
.sk.card {
  position: relative;
  background: var(--cl-glass); border: 1px solid var(--cl-glass-border);
  border-radius: var(--cl-radius); box-shadow: var(--cl-glass-shadow); padding: 20px 22px;
}
.sk.card::before {
  content: '';
  position: absolute; left: 20px; right: 20px; top: 0; height: 1px;
  background: var(--cl-glass-hi); pointer-events: none;
}
/* 微光扫过。深色下要压得比浅色板更暗 —— 骨架条是"还没有的内容"，
   亮到和真实文字一个量级就成了视觉噪音。 */
.line {
  position: relative;
  height: 14px; border-radius: 7px; margin: 10px 0;
  background: linear-gradient(90deg,
    var(--cl-skeleton-base) 25%,
    var(--cl-tint-strong) 37%,
    var(--cl-skeleton-base) 63%);
  background-size: 400% 100%; animation: shimmer 1.4s ease infinite;
}
@keyframes shimmer { 0% { background-position: 100% 0; } 100% { background-position: 0 0; } }

/* 持续动画一律可关。停下来的骨架条保持中间色，不是纯色块。 */
@media (prefers-reduced-motion: reduce) {
  .line { animation: none; background: var(--cl-neutral-bg); }
}
</style>
