<script setup lang="ts">
import { computed, ref, watch, onUnmounted, nextTick } from 'vue'
import { cleanReasoning } from '@/utils/sanitize'
import MarkdownView from '@/components/MarkdownView.vue'

/**
 * 思考中组件：卡片式，默认显示计时，可展开看推理过程、可收起。
 * - streaming=true：脉冲 + 实时计时；流式期间自动展开，结束自动收起（除非用户手动切换过）。
 * - reasoning：推理增量（可空；空则为轻量计时模式）。上屏前过 cleanReasoning，
 *   去掉控制标记和复述提示词的行。
 */
const props = withDefaults(defineProps<{ streaming?: boolean; reasoning?: string; label?: string }>(),
  { streaming: false, reasoning: '', label: '思考' })

const shown = computed(() => cleanReasoning(props.reasoning))

const open = ref(false)
const manual = ref(false)
const elapsed = ref(0)
const body = ref<HTMLElement | null>(null)
let timer: number | undefined
let startAt = 0

function startTimer() {
  startAt = performance.now() - elapsed.value * 1000
  clearInterval(timer)
  timer = window.setInterval(() => { elapsed.value = (performance.now() - startAt) / 1000 }, 100)
}
function stopTimer() { clearInterval(timer); timer = undefined }

watch(() => props.streaming, (s, prev) => {
  if (s && !prev) { elapsed.value = 0; startTimer(); if (!manual.value) open.value = true }
  if (!s && prev) { stopTimer(); if (!manual.value) open.value = false }
}, { immediate: true })

watch(() => props.reasoning, async () => {
  if (open.value) { await nextTick(); body.value?.scrollTo({ top: body.value.scrollHeight }) }
})

onUnmounted(stopTimer)

function toggle() { manual.value = true; open.value = !open.value }
const fmt = (s: number) => s.toFixed(1) + 's'
</script>

<template>
  <div class="think" :class="{ live: streaming }">
    <button class="head" @click="toggle">
      <span class="dot" :class="{ pulse: streaming }"></span>
      <span class="title">{{ streaming ? label + '中' : '已' + label }}</span>
      <span class="time">{{ fmt(elapsed) }}</span>
      <span class="spacer"></span>
      <span class="chev" :class="{ open }">
        <svg viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" /></svg>
      </span>
    </button>
    <transition name="expand">
      <div v-show="open" ref="body" class="body">
        <MarkdownView v-if="shown" :source="shown" />
        <p v-else class="muted">{{ streaming ? '正在思考…' : '这次没有分步思考过程。' }}</p>
      </div>
    </transition>
  </div>
</template>

<style scoped>
/* 思考块是"过程"不是"结论"，整体要比正文气泡沉一档；
   活跃时才靠左侧竖条发光把注意力拉过来。 */
.think {
  border: 1px solid var(--cl-glass-border); border-radius: var(--cl-radius-sm);
  background: var(--cl-row); overflow: hidden;
  border-left: 3px solid var(--cl-border-strong);
  transition: border-color .2s ease, background .2s ease, box-shadow .2s ease;
}
.think.live {
  border-left-color: var(--cl-accent);
  background: var(--cl-accent-tint);
  box-shadow: var(--cl-edge-glow-accent);
}
.head {
  display: flex; align-items: center; gap: 9px; width: 100%;
  padding: 9px 14px; border: none; background: transparent; cursor: pointer;
  font: inherit; color: var(--cl-text-2);
}
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--cl-text-3); flex-shrink: 0; }
.dot.pulse {
  background: var(--cl-accent-soft);
  box-shadow: var(--cl-dot-glow);
  animation: pulse 1s ease-in-out infinite;
}
@keyframes pulse { 0%,100% { opacity: 1; transform: scale(1); } 50% { opacity: .4; transform: scale(.75); } }
/* 持续动画一律可关；停下的点保持"亮着"，仍然表示正在进行 */
@media (prefers-reduced-motion: reduce) {
  .dot.pulse { animation: none; }
}
.title { font-weight: 600; font-size: 13.5px; }
.time { font-family: var(--cl-mono); font-size: 12.5px; color: var(--cl-text-3); }
.spacer { flex: 1; }
.chev { display: inline-flex; transition: transform .2s ease; }
.chev.open { transform: rotate(180deg); }
.chev svg { width: 16px; height: 16px; fill: none; stroke: var(--cl-text-3); stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
.body {
  padding: 4px 16px 14px 16px; max-height: 320px; overflow: auto;
  box-sizing: border-box;
  font-size: 13px; color: var(--cl-text-2); line-height: 1.7;
  border-top: 1px dashed var(--cl-row-border);
}
/* 流式内容刚开始通常只有一行占位文字；预留稳定高度，避免首批 reasoning
   到达时卡片从细条瞬间撑大。超出区域后在框内滚动，不再推动整页布局。 */
.think.live .body { height: 180px; }
.muted { color: var(--cl-text-3); margin: 8px 0 0; }
.expand-enter-active, .expand-leave-active { transition: opacity .2s ease; }
.expand-enter-from, .expand-leave-to { opacity: 0; }
</style>
