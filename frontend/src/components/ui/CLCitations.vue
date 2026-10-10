<script setup lang="ts">
import type { Citation } from '@/api/types'

defineProps<{ citations: Citation[] }>()

function pages(c: Citation): string {
  if (!c.page_start) return ''
  if (c.page_end && c.page_end !== c.page_start) return `P${c.page_start}–${c.page_end}`
  return `P${c.page_start}`
}
</script>

<template>
  <details class="evidence">
    <summary>
      <span class="summary-title">
        <span class="book" aria-hidden="true">▤</span>
        教材依据
      </span>
      <span class="count">{{ citations.length }} 条 · 点击查看原文</span>
      <span class="chevron" aria-hidden="true">⌄</span>
    </summary>

    <div class="evidence-list">
      <article v-for="(c, i) in citations" :key="`${c.source}-${c.title_path}-${i}`" class="evidence-item">
        <header>
          <span class="index">[{{ i + 1 }}]</span>
          <strong>《{{ c.source }}》</strong>
          <span v-if="c.title_path" class="location">{{ c.title_path }}</span>
          <span v-if="pages(c)" class="page">{{ pages(c) }}</span>
        </header>
        <blockquote v-if="c.excerpt">{{ c.excerpt }}</blockquote>
        <p v-else class="legacy">该历史引用仅保存了来源信息，新的回答会同时显示命中原文。</p>
      </article>
    </div>
  </details>
</template>

<style scoped>
.evidence {
  width: 100%; overflow: hidden;
  border: 1px solid var(--cl-cite-bd); border-radius: var(--cl-radius-sm);
  background: color-mix(in srgb, var(--cl-cite-bg) 62%, transparent);
}
summary {
  display: flex; align-items: center; gap: 8px; cursor: pointer; list-style: none;
  padding: 9px 11px; color: var(--cl-cite-fg); user-select: none;
}
summary::-webkit-details-marker { display: none; }
.summary-title { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 750; }
.book { font-size: 15px; line-height: 1; }
.count { margin-left: auto; font-size: 11px; color: var(--cl-text-3); }
.chevron { font-size: 16px; transition: transform .16s ease; }
.evidence[open] .chevron { transform: rotate(180deg); }
.evidence-list { display: grid; gap: 8px; padding: 0 9px 9px; }
.evidence-item {
  padding: 10px 11px; border: 1px solid var(--cl-row-border);
  border-radius: 9px; background: var(--cl-glass-2);
}
header { display: flex; align-items: baseline; flex-wrap: wrap; gap: 5px; font-size: 12px; }
.index { color: var(--cl-accent-soft); font-weight: 800; }
strong { color: var(--cl-text); }
.location { color: var(--cl-text-2); }
.page {
  padding: 1px 6px; border-radius: var(--cl-radius-pill);
  background: var(--cl-neutral-bg); color: var(--cl-text-2); font-size: 10px;
}
blockquote {
  margin: 8px 0 0; padding: 7px 9px; border-left: 2px solid var(--cl-accent);
  color: var(--cl-text-2); font-size: 12px; line-height: 1.65;
  background: var(--cl-inset); border-radius: 0 7px 7px 0;
}
.legacy { margin: 7px 0 0; color: var(--cl-text-3); font-size: 11px; }
@media (max-width: 640px) {
  .count { font-size: 10px; }
  .evidence-item { padding: 9px; }
}
</style>
