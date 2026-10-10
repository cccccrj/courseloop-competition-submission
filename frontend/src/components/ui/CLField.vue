<script setup lang="ts">
withDefaults(defineProps<{
  label?: string; modelValue?: string; type?: string; placeholder?: string; textarea?: boolean; rows?: number
}>(), { type: 'text', rows: 4 })
const emit = defineEmits<{ (e: 'update:modelValue', v: string): void; (e: 'enter'): void }>()
function onInput(e: Event) { emit('update:modelValue', (e.target as HTMLInputElement | HTMLTextAreaElement).value) }
</script>

<template>
  <label class="clf">
    <span v-if="label" class="clf-label">{{ label }}</span>
    <textarea
      v-if="textarea"
      class="clf-input clf-area"
      :rows="rows"
      :value="modelValue"
      :placeholder="placeholder"
      @input="onInput"
    />
    <input
      v-else
      class="clf-input"
      :type="type"
      :value="modelValue"
      :placeholder="placeholder"
      @input="onInput"
      @keyup.enter="$emit('enter')"
    />
  </label>
</template>

<style scoped>
/* 深色输入框皮肤，与全局 .cl-input / .cl-textarea 保持同一套取值 ——
   Review 的日期框、Solve/Practice 的文本域直接用的是那两个全局类，
   两边不一致的话同一张卡里会出现两种输入框。
   底色比卡面**更暗**：输入框是"可以往里写东西的凹槽"，压暗才有凹进去的感觉，
   提亮会让它看起来像个不可编辑的高亮块。 */
.clf { display: block; }
.clf-label { display: block; font-size: 12.5px; font-weight: 600; color: var(--cl-text-2); margin-bottom: 7px; }
.clf-input {
  width: 100%; padding: 12px 14px; font: inherit;
  border: 1px solid var(--cl-glass-border); border-radius: var(--cl-radius-sm);
  background: var(--cl-inset); color: var(--cl-text); outline: none;
  transition: border-color .18s ease, box-shadow .18s ease, background .18s ease;
}
.clf-input::placeholder { color: var(--cl-text-3); }
.clf-input:hover { border-color: var(--cl-glass-border-hover); }
.clf-input:focus {
  border-color: var(--cl-tint-border-strong);
  background: var(--cl-inset-focus);
  box-shadow: var(--cl-focus);
}
/* Chrome 自动填充会强制刷成白底黑字，在深色卡片上是个白洞。
   没有属性能直接关掉它，只能用一道超大的 inset 阴影把底色顶回去。 */
.clf-input:-webkit-autofill,
.clf-input:-webkit-autofill:focus {
  -webkit-text-fill-color: var(--cl-text);
  -webkit-box-shadow: 0 0 0 100px var(--cl-autofill-bg) inset;
  caret-color: var(--cl-text);
}
.clf-area { resize: vertical; min-height: 92px; line-height: 1.6; }
</style>
