<script setup lang="ts">
// 皮肤切换器（日间 / 护眼 / 夜间）。
//
// 结构、交互和视觉刻意与 CLCourseSwitch 同构：同样的 38px 触发器、同一套边框圆角、
// 同样的「点击别处 / Esc 关闭」。两个控件在顶栏里并排，不同构就会像两个东西凑在一起。
//
// 用的是通用表面令牌（--cl-inset / --cl-glass-border / --cl-text-*）而不是
// --cl-nav-* ：这个组件有两个挂载点 —— 应用内顶栏和登录页的 brandbar，
// 后者坐在场景层上而不是顶栏上，nav 组在那儿不成立。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useThemeStore, type ThemeMode } from '@/stores/theme'

const theme = useThemeStore()
const open = ref(false)
const root = ref<HTMLElement | null>(null)

const cur = computed(() => theme.meta)

function pick(mode: ThemeMode) {
  theme.set(mode)
  open.value = false
}

function onDocClick(e: MouseEvent) {
  if (open.value && root.value && !root.value.contains(e.target as Node)) open.value = false
}
function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') open.value = false
}
onMounted(() => {
  document.addEventListener('click', onDocClick)
  document.addEventListener('keydown', onKey)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('keydown', onKey)
})
</script>

<template>
  <div ref="root" class="ts">
    <button
      class="ts-trigger" :class="{ open }" type="button"
      :title="`当前皮肤：${cur.label}（${cur.hint}）· 点击切换`"
      aria-haspopup="listbox" :aria-expanded="open"
      @click="open = !open"
    >
      <span class="ts-ico" aria-hidden="true">{{ cur.icon }}</span>
      <span class="ts-cur">{{ cur.label }}</span>
      <svg class="ts-caret" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6" /></svg>
    </button>

    <transition name="ts-pop">
      <div v-if="open" class="ts-panel" role="listbox">
        <button
          v-for="t in theme.options" :key="t.value" type="button" role="option"
          class="ts-item" :class="{ on: t.value === theme.mode }"
          :aria-selected="t.value === theme.mode"
          @click="pick(t.value)"
        >
          <span class="ts-item-ico" aria-hidden="true">{{ t.icon }}</span>
          <span class="ts-item-text">
            <span class="ts-name">{{ t.label }}</span>
            <span class="ts-hint">{{ t.hint }}</span>
          </span>
          <!-- 勾在最右，占位固定：不给未选中项留位置的话，选中项会把整行挤宽一点 -->
          <span class="ts-check" aria-hidden="true">{{ t.value === theme.mode ? '✓' : '' }}</span>
        </button>
      </div>
    </transition>
  </div>
</template>

<style scoped>
.ts { position: relative; }

.ts-trigger {
  display: inline-flex; align-items: center; gap: 7px;
  height: 38px; padding: 0 10px 0 11px; white-space: nowrap; flex-shrink: 0;
  border: 1px solid var(--cl-glass-border); border-radius: var(--cl-radius-sm);
  background: var(--cl-inset); color: var(--cl-text-2);
  font: inherit; font-size: 13.5px; font-weight: 700; cursor: pointer;
  transition: color .14s ease, border-color .14s ease, background .14s ease, box-shadow .14s ease;
}
.ts-trigger:hover,
.ts-trigger.open {
  color: var(--cl-brand-600);
  border-color: var(--cl-tint-border-strong);
  background: var(--cl-tint-strong);
  box-shadow: var(--cl-glow);
}
.ts-trigger:focus-visible { outline: 2px solid var(--cl-brand); outline-offset: 2px; }

.ts-ico { font-size: 14px; line-height: 1; }
.ts-cur { color: var(--cl-text); }
.ts-trigger:hover .ts-cur,
.ts-trigger.open .ts-cur { color: var(--cl-brand-700); }
.ts-caret {
  width: 15px; height: 15px; fill: none; stroke: currentColor; stroke-width: 2.2;
  stroke-linecap: round; stroke-linejoin: round; transition: transform .18s ease;
}
.ts-trigger.open .ts-caret { transform: rotate(180deg); }

/* 与 CLSelect / CLCourseSwitch 的下拉同一配方：底色近乎不透明负责遮挡，
   模糊只负责质感。 */
.ts-panel {
  position: absolute; right: 0; top: calc(100% + 8px); z-index: 40;
  min-width: 208px; padding: 7px;
  background: var(--cl-popover);
  backdrop-filter: var(--cl-blur);
  -webkit-backdrop-filter: var(--cl-blur);
  border: 1px solid var(--cl-popover-border);
  border-radius: var(--cl-radius); box-shadow: var(--cl-shadow-lg);
}
.ts-item {
  display: flex; align-items: center; gap: 9px; width: 100%;
  padding: 8px 10px; border: 0; border-radius: var(--cl-radius-sm);
  background: transparent; color: var(--cl-text-2);
  font: inherit; font-size: 13.5px; font-weight: 600; cursor: pointer; text-align: left;
  transition: background .14s ease, color .14s ease;
}
.ts-item:hover { background: var(--cl-tint); color: var(--cl-text); }
.ts-item.on { background: var(--cl-tint-strong); color: var(--cl-brand-700); }
.ts-item:focus-visible { outline: 2px solid var(--cl-brand); outline-offset: -2px; }
.ts-item-ico { font-size: 15px; line-height: 1; width: 16px; text-align: center; flex: none; }
.ts-item-text { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 1px; }
.ts-name { font-weight: 700; }
.ts-hint { font-size: 11.5px; font-weight: 500; color: var(--cl-text-3); }
.ts-item.on .ts-hint { color: var(--cl-text-2); }
.ts-check { flex: none; width: 12px; font-size: 12px; color: var(--cl-brand-600); }

.ts-pop-enter-active, .ts-pop-leave-active { transition: opacity .14s ease, transform .14s ease; }
.ts-pop-enter-from, .ts-pop-leave-to { opacity: 0; transform: translateY(-4px); }

/* 手机宽度下只留图标，档位名在展开面板里（按钮的 title 仍报当前档位） */
@media (max-width: 420px) {
  .ts-cur { display: none; }
}

@media (prefers-reduced-motion: reduce) {
  .ts-pop-enter-active, .ts-pop-leave-active { transition: none; }
  .ts-caret { transition: none; }
}
</style>
