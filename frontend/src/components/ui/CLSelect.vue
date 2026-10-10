<script setup lang="ts">
// 可搜索的分组下拉，形态与 CLField 对齐（同样的标签/边框/圆角/聚焦光圈），
// 放在表单里和输入框并排才像一套东西。
//
// 原生 <option> 不能内嵌搜索框，分组样式也因系统而异，因此使用可筛选组件。
//
// allowCreate 让静态选项只作为输入便利，找不到时仍可保存自定义内容。
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

interface Group { label: string; options: string[] }

const props = withDefaults(defineProps<{
  modelValue?: string
  label?: string
  placeholder?: string
  groups: Group[]
  allowCreate?: boolean
}>(), { modelValue: '', placeholder: '请选择', allowCreate: true })

const emit = defineEmits<{ (e: 'update:modelValue', v: string): void }>()

const open = ref(false)
const query = ref('')
const root = ref<HTMLElement | null>(null)
const search = ref<HTMLInputElement | null>(null)

const filtered = computed<Group[]>(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return props.groups
  return props.groups
    .map((g) => ({ label: g.label, options: g.options.filter((o) => o.toLowerCase().includes(q)) }))
    .filter((g) => g.options.length > 0)
})

const hitCount = computed(() => filtered.value.reduce((n, g) => n + g.options.length, 0))

// 输入的内容与名单里某项完全一致时不再提示"新建"，否则会出现两条一模一样的候选
const canCreate = computed(() => {
  const q = query.value.trim()
  if (!props.allowCreate || !q) return false
  return !props.groups.some((g) => g.options.some((o) => o === q))
})

function toggle() {
  open.value = !open.value
  if (open.value) {
    query.value = ''
    nextTick(() => search.value?.focus())
  }
}

function pick(v: string) {
  emit('update:modelValue', v)
  open.value = false
  query.value = ''
}

function clear() {
  emit('update:modelValue', '')
  open.value = false
}

// 回车取第一个命中项；没命中但允许自建时，就把输入的内容本身存下来
function onEnter() {
  const first = filtered.value[0]?.options[0]
  if (first) pick(first)
  else if (canCreate.value) pick(query.value.trim())
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
  <div ref="root" class="cls">
    <span v-if="label" class="cls-label">{{ label }}</span>

    <button
      type="button" class="cls-trigger" :class="{ open, empty: !modelValue }"
      aria-haspopup="listbox" :aria-expanded="open" @click="toggle"
    >
      <span class="cls-val">{{ modelValue || placeholder }}</span>
      <span v-if="modelValue" class="cls-clear" title="清空" @click.stop="clear">×</span>
      <svg class="cls-caret" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6" /></svg>
    </button>

    <transition name="cls-pop">
      <div v-if="open" class="cls-panel" role="listbox">
        <input
          ref="search" v-model="query" class="cls-search" type="text"
          placeholder="输入关键词筛选，如「计算机」" @keyup.enter="onEnter"
        />

        <div class="cls-list">
          <template v-for="g in filtered" :key="g.label">
            <div class="cls-group">{{ g.label }}</div>
            <button
              v-for="o in g.options" :key="o" type="button" role="option"
              class="cls-item" :class="{ on: o === modelValue }" :aria-selected="o === modelValue"
              @click="pick(o)"
            >
              <span class="cls-bar" />
              <span class="cls-name">{{ o }}</span>
            </button>
          </template>

          <div v-if="!hitCount && !canCreate" class="cls-none">没有匹配的选项</div>
        </div>

        <button v-if="canCreate" type="button" class="cls-create" @click="pick(query.trim())">
          <span class="cls-plus">＋</span>使用「{{ query.trim() }}」
        </button>
      </div>
    </transition>
  </div>
</template>

<style scoped>
.cls { position: relative; }
.cls-label { display: block; font-size: 12.5px; font-weight: 600; color: var(--cl-text-2); margin-bottom: 7px; }

/* 与 CLField 的 .clf-input 同尺寸同边框同底色，两者并排不会一高一低、一深一浅 */
.cls-trigger {
  display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 12px 14px; font: inherit; text-align: left; cursor: pointer;
  border: 1px solid var(--cl-glass-border); border-radius: var(--cl-radius-sm);
  background: var(--cl-inset); color: var(--cl-text);
  transition: border-color .18s ease, box-shadow .18s ease, background .18s ease;
}
.cls-trigger:hover { border-color: var(--cl-glass-border-hover); }
.cls-trigger.open {
  border-color: var(--cl-tint-border-strong);
  background: var(--cl-inset-focus);
  box-shadow: var(--cl-focus);
}
.cls-trigger:focus-visible { outline: 2px solid var(--cl-brand); outline-offset: 2px; }
.cls-trigger.empty .cls-val { color: var(--cl-text-3); }

.cls-val { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cls-clear {
  flex: none; width: 18px; height: 18px; line-height: 16px; text-align: center;
  border-radius: 50%; font-size: 15px; color: var(--cl-text-3);
  transition: background .14s ease, color .14s ease;
}
.cls-clear:hover { background: var(--cl-tint-strong); color: var(--cl-text); }
.cls-caret {
  width: 15px; height: 15px; flex: none; fill: none; stroke: var(--cl-text-3); stroke-width: 2.2;
  stroke-linecap: round; stroke-linejoin: round; transition: transform .18s ease;
}
.cls-trigger.open .cls-caret { transform: rotate(180deg); stroke: var(--cl-brand-600); }

/* 下拉面板是少数几个**该**用 backdrop-filter 的表面：它盖在正文上面，
   底下是要被读的文字，只靠半透明会串味。这类浮层全站一共三处
   （本面板、科目切换器、顶栏），加模糊的开销可控。
   底色也压到接近不透明，模糊只负责质感，不负责遮挡。 */
.cls-panel {
  position: absolute; left: 0; right: 0; top: calc(100% + 6px); z-index: 60;
  padding: 7px;
  background: var(--cl-popover);
  backdrop-filter: var(--cl-blur);
  -webkit-backdrop-filter: var(--cl-blur);
  border: 1px solid var(--cl-popover-border); border-radius: var(--cl-radius);
  box-shadow: var(--cl-shadow-lg);
}
.cls-search {
  width: 100%; padding: 9px 11px; font: inherit; font-size: 13.5px;
  border: 1px solid var(--cl-glass-border); border-radius: var(--cl-radius-sm);
  background: var(--cl-inset); color: var(--cl-text); outline: none;
  transition: border-color .18s ease, background .18s ease;
}
.cls-search::placeholder { color: var(--cl-text-3); }
.cls-search:focus { border-color: var(--cl-tint-border-strong); background: var(--cl-inset-focus); }

/* 限高滚动：26 项全展开会顶穿视口，把下方字段整个盖住 */
.cls-list { max-height: 232px; overflow-y: auto; margin-top: 6px; }
.cls-group {
  font-size: 11px; font-weight: 700; letter-spacing: .6px; color: var(--cl-text-3);
  padding: 8px 10px 5px;
}
.cls-item {
  position: relative; display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 8px 10px; border: 0; border-radius: var(--cl-radius-sm);
  background: transparent; color: var(--cl-text-2);
  font: inherit; font-size: 13.5px; font-weight: 600; cursor: pointer; text-align: left;
  transition: background .14s ease, color .14s ease;
}
.cls-item:hover { background: var(--cl-tint); color: var(--cl-text); }
.cls-item.on { background: var(--cl-tint-strong); color: var(--cl-brand-700); font-weight: 700; }
.cls-item:focus-visible { outline: 2px solid var(--cl-brand); outline-offset: -2px; }

/* 选中项左侧竖条，与侧边栏 .nav-item.active、科目切换器同一视觉语言 */
.cls-bar {
  width: 3px; height: 15px; border-radius: 3px; flex: none;
  background: var(--cl-accent-soft); transform: scaleY(0); transition: transform .16s ease;
}
.cls-item.on .cls-bar { transform: scaleY(1); }
.cls-name { flex: 1; }

.cls-none { padding: 14px 10px; text-align: center; font-size: 13px; color: var(--cl-text-3); }

.cls-create {
  display: flex; align-items: center; gap: 6px; width: 100%; margin-top: 4px;
  padding: 9px 10px; border: 0; border-top: 1px solid var(--cl-glass-border);
  border-radius: 0 0 var(--cl-radius-sm) var(--cl-radius-sm);
  background: transparent; color: var(--cl-brand-600);
  font: inherit; font-size: 13px; font-weight: 700; cursor: pointer; text-align: left;
}
.cls-create:hover { background: var(--cl-tint); color: var(--cl-text); }
.cls-plus { font-size: 14px; }

.cls-pop-enter-active, .cls-pop-leave-active { transition: opacity .16s ease, transform .16s ease; }
.cls-pop-enter-from, .cls-pop-leave-to { opacity: 0; transform: translateY(-6px); }
</style>
