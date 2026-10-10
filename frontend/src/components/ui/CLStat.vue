<script setup lang="ts">
defineProps<{ value: string | number; label: string; accent?: boolean }>()
</script>

<template>
  <div class="stat" :class="{ accent }">
    <div class="num">{{ value }}</div>
    <div class="label">{{ label }}</div>
  </div>
</template>

<style scoped>
/* 紧凑版（第二阶段 · 全站规范）。原先 32px 大数字 + 22px 内边距，四张一排能占掉首屏六分之一，
   而这些数是"看一眼"的背景信息，不是页面主角；还带悬停上浮，让人以为点得开。
   现在：数字 24px、左对齐（读数先看数字再看标签，左对齐扫得快）、不上浮。 */
.stat {
  position: relative; overflow: hidden;
  background: var(--cl-glass-2); border: 1px solid var(--cl-glass-border);
  border-radius: var(--cl-radius-sm); box-shadow: var(--cl-glass-shadow);
  padding: 12px 16px; text-align: left;
}
/* 顶缘高光。数字卡通常四个一排，高光让它们连成一条线 */
.stat::before {
  content: '';
  position: absolute; left: 14px; right: 14px; top: 0; height: 1px;
  background: var(--cl-glass-hi); pointer-events: none;
}
/* accent 是每组里唯一的重点项：只换边框与数字颜色，不再垫大块辉光 */
.stat.accent { border-color: var(--cl-tint-border); }
.num {
  position: relative;
  font-size: 24px; font-weight: 800; line-height: 1.1; letter-spacing: .2px;
  color: var(--cl-text);
}
.stat.accent .num { color: var(--cl-brand-600); }
.label { position: relative; font-size: 12.5px; color: var(--cl-text-3); margin-top: 4px; }
</style>
