<script setup lang="ts">
// 顶栏科目切换器。
//
// 原先是原生 <select> —— 各系统渲染不一（Windows 上是灰底方框），既不跟随
// 深色主题，也放不下分值。408 四门分值差别很大（45/45/35/25），复习时按分值
// 权衡投入是刚需，所以这里把分值一并露出来，而原生 <option> 做不到。
//
// 保持 38px 高，并与顶栏其他控件共用边框、圆角和 hover 语言，
// 两个控件并排才像一套东西。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useCourseStore } from '@/stores/course'

const courseStore = useCourseStore()
const open = ref(false)
const root = ref<HTMLElement | null>(null)

const list = computed(() => courseStore.list)
const info = computed(() => courseStore.info)

function pick(id: string) {
  courseStore.set(id)
  open.value = false
}

// 点击别处 / Esc 关闭：浮层不响应这两个动作会让人觉得“关不掉”
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
  <div ref="root" class="cs">
    <button
      class="cs-trigger" :class="{ open }" type="button"
      :title="`当前科目：${info.display}（${info.score} 分）· 点击切换`"
      aria-haspopup="listbox" :aria-expanded="open"
      @click="open = !open"
    >
      <span class="cs-cur">{{ info.short }}</span>
      <span class="cs-score-mini">{{ info.score }} 分</span>
      <svg class="cs-caret" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6" /></svg>
    </button>

    <transition name="cs-pop">
      <div v-if="open" class="cs-panel" role="listbox">
        <div class="cs-head">408 统考 · 四门（数字为该门在 150 分里的分值）</div>
        <button
          v-for="c in list" :key="c.id" type="button" role="option"
          class="cs-item" :class="{ on: c.id === courseStore.current }"
          :aria-selected="c.id === courseStore.current"
          @click="pick(c.id)"
        >
          <span class="cs-bar" />
          <span class="cs-name">{{ c.short }}</span>
          <!-- 分值条：四门差别大，用长度让权重一眼可见，数字再兜底 -->
          <span class="cs-meter"><i :style="{ width: (c.score / 45 * 100) + '%' }" /></span>
          <span class="cs-score">{{ c.score }} 分</span>
        </button>
      </div>
    </transition>
  </div>
</template>

<style scoped>
.cs { position: relative; }

/* 触发器坐在顶栏上，顶栏本身已经是半透明模糊的，所以这里只需要一层比顶栏
   稍暗的玻璃就能浮出来，不用再叠模糊。 */
.cs-trigger {
  display: inline-flex; align-items: center; gap: 7px;
  height: 38px; padding: 0 10px 0 11px; white-space: nowrap; flex-shrink: 0;
  border: 1px solid var(--cl-glass-border); border-radius: var(--cl-radius-sm);
  background: var(--cl-inset); color: var(--cl-text-2);
  font: inherit; font-size: 13.5px; font-weight: 700; cursor: pointer;
  transition: color .14s ease, border-color .14s ease, background .14s ease, box-shadow .14s ease;
}
.cs-trigger:hover,
.cs-trigger.open {
  color: var(--cl-brand-600);
  border-color: var(--cl-tint-border-strong);
  background: var(--cl-tint);
  box-shadow: var(--cl-glow);
}
.cs-trigger:focus-visible { outline: 2px solid var(--cl-brand); outline-offset: 2px; }

.cs-ico { font-size: 14px; line-height: 1; }
.cs-cur { color: var(--cl-text); }
.cs-trigger:hover .cs-cur,
.cs-trigger.open .cs-cur { color: var(--cl-brand-700); }
/* 收起态也带分值：不用展开就知道当前科目的权重 */
.cs-score-mini {
  font-size: 11px; font-weight: 800; line-height: 1;
  padding: 3px 6px; border-radius: var(--cl-radius-pill);
  background: var(--cl-cite-bg); color: var(--cl-cite-fg);
  border: 1px solid var(--cl-cite-bd);
}
.cs-caret {
  width: 15px; height: 15px; fill: none; stroke: currentColor; stroke-width: 2.2;
  stroke-linecap: round; stroke-linejoin: round; transition: transform .18s ease;
}
.cs-trigger.open .cs-caret { transform: rotate(180deg); }

/* 与 CLSelect 的下拉面板同一配方：浮层盖住正文，是全站少数该用 backdrop-filter
   的表面之一。底色压到近乎不透明负责遮挡，模糊只负责质感。 */
.cs-panel {
  position: absolute; right: 0; top: calc(100% + 8px); z-index: 40;
  min-width: 216px; padding: 7px;
  background: var(--cl-popover);
  backdrop-filter: var(--cl-blur);
  -webkit-backdrop-filter: var(--cl-blur);
  border: 1px solid var(--cl-popover-border);
  border-radius: var(--cl-radius); box-shadow: var(--cl-shadow-lg);
}
.cs-head {
  font-size: 11px; font-weight: 700; letter-spacing: .6px; text-transform: uppercase;
  color: var(--cl-text-3); padding: 6px 10px 8px;
}

.cs-item {
  position: relative; display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 9px 10px; border: 0; border-radius: var(--cl-radius-sm);
  background: transparent; color: var(--cl-text-2);
  font: inherit; font-size: 13.5px; font-weight: 600; cursor: pointer; text-align: left;
  transition: background .14s ease, color .14s ease;
}
.cs-item:hover { background: var(--cl-tint); color: var(--cl-text); }
.cs-item.on { background: var(--cl-tint-strong); color: var(--cl-brand-700); font-weight: 700; }
.cs-item:focus-visible { outline: 2px solid var(--cl-brand); outline-offset: -2px; }

/* 选中项的左侧竖条，与侧边栏 .nav-item.active 同一视觉语言 */
.cs-bar {
  width: 3px; height: 16px; border-radius: 3px; flex: none;
  background: var(--cl-accent-soft); transform: scaleY(0); transition: transform .16s ease;
  box-shadow: var(--cl-dot-glow);
}
.cs-item.on .cs-bar { transform: scaleY(1); }

.cs-name { flex: 1; white-space: nowrap; }
.cs-meter {
  width: 34px; height: 4px; flex: none; border-radius: 2px;
  background: var(--cl-inset); box-shadow: 0 0 0 1px var(--cl-glass-border) inset;
}
.cs-meter i { display: block; height: 100%; border-radius: 2px; background: linear-gradient(90deg, var(--cl-brand), var(--cl-accent)); }
.cs-score { font-size: 11.5px; font-weight: 800; color: var(--cl-text-3); flex: none; width: 34px; text-align: right; }
.cs-item.on .cs-score { color: var(--cl-brand-700); }

.cs-pop-enter-active, .cs-pop-leave-active { transition: opacity .16s ease, transform .16s ease; }
.cs-pop-enter-from, .cs-pop-leave-to { opacity: 0; transform: translateY(-6px); }

/* 手机宽度下顶栏还要放汉堡按钮和主题切换，分值徽标让出来（展开面板里仍有） */
@media (max-width: 420px) {
  .cs-score-mini { display: none; }
}
</style>
