<script setup lang="ts">
withDefaults(defineProps<{ variant?: 'primary' | 'ghost' | 'text'; disabled?: boolean; block?: boolean }>(),
  { variant: 'primary', disabled: false, block: false })
defineEmits<{ (e: 'click', ev: MouseEvent): void }>()
</script>

<template>
  <button :class="['clb', variant, { block }]" :disabled="disabled" @click="$emit('click', $event)">
    <slot />
  </button>
</template>

<style scoped>
/* position/overflow 是掠过高光 ::after 的定位与裁切基准，三种变体共用 */
.clb {
  position: relative; overflow: hidden;
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  padding: 11px 20px; border: none; cursor: pointer; font: inherit; font-weight: 700; font-size: 14px;
  border-radius: var(--cl-radius-sm);
  transition: transform .12s ease, box-shadow .12s ease, filter .12s ease, background .12s ease, color .12s ease;
}
.clb.block { width: 100%; }
/* 禁用态不能靠 opacity。主按钮整体降到 .5 后，按钮面与卡面只差 1.61:1 ——
   先消失的是按钮的"形状"，字反而还剩 4.19。结果是读得出字、看不出这是个按钮。
   改成每个变体各自定义禁用表面：压平填充（去掉渐变、辉光和扫光）+ 保留一条发丝描边
   把轮廓勾住 + 字降一级。禁用感由"平"传达，不由"淡"传达。 */
.clb:disabled { cursor: not-allowed; transform: none !important; }
.clb.primary:disabled {
  background: var(--cl-tint-strong);
  color: var(--cl-text-3);
  box-shadow: 0 0 0 1px var(--cl-glass-border) inset !important;
}
.clb.ghost:disabled {
  background: var(--cl-tint);
  color: var(--cl-text-3);
  box-shadow: 0 0 0 1px var(--cl-tint) inset !important;
}
/* text 变体没有填充，也就没有"形状"可保，唯一的杠杆就是颜色；
   而它启用态本来就是 text-3，所以这里只能再降一档才区分得开。
   这是三个变体里唯一还靠"淡"表达禁用的，因为确实没有别的可用信号。 */
.clb.text:disabled { color: var(--cl-text-4); background: transparent; box-shadow: none !important; }
.clb:focus-visible { outline: 2px solid var(--cl-brand); outline-offset: 2px; }

.clb.primary {
  color: var(--cl-on-brand); background: var(--cl-brand-grad);
  box-shadow: var(--cl-glow);
}
.clb.primary:hover { transform: translateY(-1px); box-shadow: var(--cl-btn-shadow-hover); }
.clb.primary:active { transform: translateY(0); }
/* 扫光：从登录页主按钮提上来，让全站主按钮共用同一个悬停语言。
   skew + 只走 left，不碰 filter/opacity —— 换成 filter 会给按钮建合成层，
   一屏十几个按钮时不划算。 */
.clb.primary::after {
  display: none;   /* 悬停扫光去掉（去 AI 味）：主按钮靠颜色和位置就够醒目，不需要闪一下 */
  content: '';
  position: absolute; top: 0; left: -60%; width: 45%; height: 100%;
  background: linear-gradient(100deg, transparent, var(--cl-btn-sheen), transparent);
  transform: skewX(-18deg); transition: left .55s ease;
  pointer-events: none;
}
.clb.primary:hover::after { left: 120%; }
.clb.primary:disabled::after { display: none; }

/* ghost：深色下"更浅的实底"会显得比主按钮还抢眼，改成靛蓝薄膜 + 发丝描边 */
.clb.ghost {
  color: var(--cl-brand-600);
  background: var(--cl-tint);
  box-shadow: 0 0 0 1px var(--cl-glass-border) inset;
}
.clb.ghost:hover {
  color: var(--cl-brand-700); background: var(--cl-tint-strong);
  box-shadow: 0 0 0 1px var(--cl-tint-border-strong) inset;
}

.clb.text { color: var(--cl-text-3); background: transparent; padding: 6px 8px; font-weight: 600; }
.clb.text:hover { color: var(--cl-brand-600); background: var(--cl-tint); }

@media (prefers-reduced-motion: reduce) {
  .clb { transition: none; }
  .clb.primary::after { display: none; }
  .clb.primary:hover, .clb:hover { transform: none; }
}
</style>
