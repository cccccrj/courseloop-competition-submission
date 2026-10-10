<script setup lang="ts">
withDefaults(defineProps<{ title?: string; sub?: string; hoverable?: boolean; pad?: boolean }>(),
  { hoverable: false, pad: true })
</script>

<template>
  <div class="clcard" :class="{ hoverable, pad }">
    <div v-if="title || $slots.actions || $slots.header" class="head">
      <slot name="header">
        <div class="head-text">
          <div class="title">{{ title }}</div>
          <div v-if="sub" class="sub">{{ sub }}</div>
        </div>
      </slot>
      <div v-if="$slots.actions" class="actions"><slot name="actions" /></div>
    </div>
    <slot />
  </div>
</template>

<style scoped>
/* 玻璃卡面。刻意不用 backdrop-filter —— 学习中心一屏就有六七张卡，Practice 更多，
   每张一个模糊层的合成开销是实打实的。半透明渐变 + 发丝描边 + 顶缘高光 + 分层阴影
   压在 MainLayout 的氛围底上，肉眼几乎分辨不出，而且不花性能。
   真正需要模糊的是会盖住内容的表面（顶栏、侧栏、下拉面板），那些自己加 --cl-blur。 */
.clcard {
  position: relative;
  background: var(--cl-glass); border: 1px solid var(--cl-glass-border);
  border-radius: var(--cl-radius); box-shadow: var(--cl-glass-shadow);
  transition: box-shadow .18s ease, transform .18s ease, border-color .18s ease;
}
/* 顶缘高光：玻璃质感全靠这一条，去掉就退回普通半透明块。
   左右各缩进 20px 让高光两端自然收掉，拉满会在圆角处露出直角的接缝。 */
.clcard::before {
  content: '';
  position: absolute; left: 20px; right: 20px; top: 0; height: 1px;
  background: var(--cl-glass-hi);
  pointer-events: none;
}
.clcard.pad { padding: 20px 22px; }
.clcard.hoverable:hover {
  box-shadow: var(--cl-shadow-lg); transform: translateY(-2px);
  border-color: var(--cl-glass-border-hover);
}
.head { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 14px; }
.title { font-weight: 700; font-size: 15px; letter-spacing: .2px; }
.sub { font-size: 13px; color: var(--cl-text-3); margin-top: 2px; }
.actions { flex-shrink: 0; }
</style>
