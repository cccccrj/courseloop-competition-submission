<script setup lang="ts">
defineProps<{ title: string; sub?: string }>()
</script>

<template>
  <div class="clph">
    <div class="clph-text">
      <h1 class="clph-title">{{ title }}</h1>
      <p v-if="sub" class="clph-sub" :title="sub">{{ sub }}</p>
    </div>
    <div v-if="$slots.actions" class="clph-actions"><slot name="actions" /></div>
  </div>
</template>

<style scoped>
.clph { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 22px; }
.clph-text { min-width: 0; }
/* 纯色，不再渐变裁字（去 AI 味）：渐变标题在每页顶上重复出现，是最容易被认出的"模板感" */
.clph-title { font-size: 22px; font-weight: 800; margin: 0 0 5px; letter-spacing: .2px; color: var(--cl-text); }
/* 副标题只留一行（全站规范）：原先每页一段两行的说明，走查里被评"一堆看不懂的字"。
   超出的截断，完整文字放在 title 里，悬停可见。 */
.clph-sub {
  margin: 0; font-size: 13.5px; color: var(--cl-text-3); line-height: 1.55;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
/* 徽标常常两三个并排（科目 / 分值 / 连续天数），要能换行不能挤爆标题 */
.clph-actions { flex-shrink: 0; display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }

@media (max-width: 620px) {
  .clph { flex-direction: column; gap: 12px; }
  .clph-actions { max-width: 100%; }
}
</style>
