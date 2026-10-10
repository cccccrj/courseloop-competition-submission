<script setup lang="ts">
import { computed } from 'vue'

// tone 是语义而不是色相：brand=正常推进，warn=需要关注，err=概念错误。
// （原先叫 'mint'，是浅色薄荷绿时期的命名；那个色相已经不存在了，
//  留着会让人以为能选出一个绿色的进度条。）
const props = withDefaults(defineProps<{ value: number; label?: string; right?: string; tone?: 'brand' | 'warn' | 'err' }>(),
  { value: 0, tone: 'brand' })

const pct = computed(() => Math.max(0, Math.min(100, props.value || 0)))
</script>

<template>
  <div class="prog">
    <div v-if="label || right" class="row">
      <span class="label">{{ label }}</span>
      <span class="right">{{ right }}</span>
    </div>
    <div class="track">
      <div class="fill" :class="[tone, { zero: pct === 0 }]" :style="{ width: pct + '%' }"></div>
    </div>
  </div>
</template>

<style scoped>
.prog { width: 100%; }
.row { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-bottom: 6px; }
/* 知识点名可以很长（"最短路径 Dijkstra 算法"），不截断会把右侧状态挤出行外 */
.label { font-weight: 600; font-size: 13.5px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.right { font-size: 12.5px; color: var(--cl-text-3); flex: none; }

/* 轨道压暗到接近页面底色，让填充条自己发光。原先的 --cl-surface-3 是"比卡面更亮"
   的灰，掌握度低时反而是空轨道在抢注意力。
   这里**不能**用 overflow:hidden —— 它会把填充条的辉光连同溢出一起裁掉，
   辉光就完全看不见了。填充条自己有 border-radius，不靠父级裁形状。 */
.track {
  height: 8px; border-radius: 999px;
  background: var(--cl-inset);
  box-shadow: 0 0 0 1px var(--cl-glass-border) inset;
}
/* 辉光走 box-shadow 而不是 filter:drop-shadow：掌握度列表一屏能有二十来条，
   drop-shadow 每条都要一次离屏合成，box-shadow 不用。 */
.fill {
  height: 100%; border-radius: 999px;
  transition: width .5s cubic-bezier(.22,.61,.36,1);
}
.fill.brand { background: linear-gradient(90deg, var(--cl-brand), var(--cl-accent)); box-shadow: var(--cl-glow); }
.fill.warn { background: linear-gradient(90deg, var(--cl-warn-deep), var(--cl-warn)); box-shadow: var(--cl-glow-warn); }
.fill.err { background: linear-gradient(90deg, var(--cl-err-deep), var(--cl-err)); box-shadow: var(--cl-glow-err); }
/* 0% 的元素宽度为零但 box-shadow 照样画，会在轨道最左端留一个光点，
   看起来像"已经有一点进度了"—— 未开始的知识点必须是空的。 */
.fill.zero { box-shadow: none; }

@media (prefers-reduced-motion: reduce) {
  .fill { transition: none; }
}
</style>
