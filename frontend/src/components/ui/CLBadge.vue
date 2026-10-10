<script setup lang="ts">
// 语义徽标：ok=已验证 warn=未验证 err=错因 cite=教材引用
withDefaults(defineProps<{ variant?: 'ok' | 'warn' | 'err' | 'cite' | 'neutral' }>(), { variant: 'neutral' })
</script>

<template>
  <span class="clbadge" :class="variant"><slot /></span>
</template>

<style scoped>
/* 深色板下这几个色对是"深底 + 亮字"，与浅色板的"浅底 + 深字"正好相反，
   取值统一放在 theme.css 的语义色区，这里只引用。
   ok 从 --cl-brand-100 改成 --cl-ok-bg：语义上 ok=已验证/已解决，之前借用品牌靛蓝
   导致它和 cite（教材引用，本来就是靛蓝）在一行里几乎分不出来。 */
.clbadge {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 3px 11px; border-radius: var(--cl-radius-pill);
  border: 1px solid transparent;
  font-size: 12px; font-weight: 600; white-space: nowrap;
}
.neutral { background: var(--cl-neutral-bg); color: var(--cl-text-2); border-color: var(--cl-neutral-bd); }
.ok { background: var(--cl-ok-bg); color: var(--cl-ok-fg); border-color: var(--cl-ok-bd); }
.warn { background: var(--cl-warn-bg); color: var(--cl-warn-fg); border-color: var(--cl-warn-bd); }
.err { background: var(--cl-err-bg); color: var(--cl-err-fg); border-color: var(--cl-err-bd); }
.cite { background: var(--cl-cite-bg); color: var(--cl-cite-fg); border-color: var(--cl-cite-bd); }
</style>
