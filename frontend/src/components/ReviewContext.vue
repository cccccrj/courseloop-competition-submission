<script setup lang="ts">
/**
 * 一条待复核错题的判断依据：题目 → 学生的作答 → AI 的判定。
 *
 * 此前复核队列只有一句截到 160 字的诊断，助教是在看不到题目、看不到学生写了什么的
 * 情况下点「确认 / 改判」（docs/34 §3.6）。顺序刻意是"先题目后判定"：
 * 助教该先自己看一眼学生错在哪，再对照 AI 怎么说，而不是先被 AI 的结论带着走。
 *
 * 总览的「最近错题」与「复核队列」共用这一个组件 —— 两处曾经一处用 MarkdownView、
 * 一处直接插值，同一条诊断在两个 tab 里长得不一样。
 */
import MarkdownView from '@/components/MarkdownView.vue'
import CLBadge from '@/components/ui/CLBadge.vue'

defineProps<{ r: any; compact?: boolean }>()
</script>

<template>
  <div class="ctx" :class="{ compact }">
    <div class="ctx-reason">
      <CLBadge :variant="r.review_reason === 'student_request' ? 'err'
        : r.review_reason === 'manual' ? 'neutral' : 'warn'">{{ r.review_reason_label || '普通错题' }}</CLBadge>
      <span v-if="r.knowledge" class="ctx-kp">{{ r.knowledge }}</span>
    </div>

    <div class="ctx-block">
      <span class="ctx-label">题目</span>
      <div v-if="r.problem_stem" class="ctx-body"><MarkdownView :source="r.problem_stem" /></div>
      <p v-else class="ctx-missing">这条记录没有保存题目（旧版本写入的记录）。</p>
    </div>

    <div class="ctx-block">
      <span class="ctx-label">学生的作答</span>
      <div v-if="r.student_answer" class="ctx-body answer"><MarkdownView :source="r.student_answer" /></div>
      <p v-else class="ctx-missing">没有保存作答原文（旧记录或演示数据）。</p>
    </div>

    <div class="ctx-block">
      <span class="ctx-label">AI 判定</span>
      <div class="ctx-body">
        <div class="ctx-verdict">
          <CLBadge variant="warn">{{ r.error_name }}</CLBadge>
          <span v-if="r.ai_error_name" class="ctx-was">AI 原判：{{ r.ai_error_name }}</span>
        </div>
        <MarkdownView v-if="r.diagnosis" :source="r.diagnosis" />
        <p v-if="r.follow_up" class="ctx-follow">建议处置：{{ r.follow_up }}</p>
        <!-- 代码运行失败的原始报错（I-05）：诊断里只写中文说明，原文默认收起，
             助教想确认学生错在哪一行时再展开。学生端接口不下发这个字段。 -->
        <details v-if="r.tech_detail && !compact" class="ctx-tech">
          <summary>技术详情</summary>
          <pre>{{ r.tech_detail }}</pre>
        </details>
      </div>
    </div>

    <details v-if="r.reference && !compact" class="ctx-ref">
      <summary>参考答案</summary>
      <MarkdownView :source="r.reference" />
    </details>
  </div>
</template>

<style scoped>
.ctx { display: flex; flex-direction: column; gap: 10px; margin: 8px 0 4px; }
.ctx-reason { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ctx-kp { font-size: 12px; color: var(--cl-text-3); }
.ctx-block { display: grid; grid-template-columns: 76px 1fr; gap: 10px; align-items: start; }
.ctx-label { font-size: 12px; font-weight: 700; color: var(--cl-text-3); padding-top: 3px; }
.ctx-body {
  min-width: 0; font-size: 13.5px; color: var(--cl-text-2);
  padding: 8px 10px; border-radius: var(--cl-radius-sm);
  background: var(--cl-row); border: 1px solid var(--cl-row-border);
}
.ctx-body.answer { border-left: 2px solid var(--cl-accent); }
.ctx-missing { margin: 0; padding-top: 3px; font-size: 12.5px; color: var(--cl-text-3); }
.ctx-verdict { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.ctx-was { font-size: 12px; color: var(--cl-text-3); }
.ctx-follow { margin: 6px 0 0; font-size: 12.5px; color: var(--cl-text-3); }
.ctx-ref summary { cursor: pointer; font-size: 12.5px; color: var(--cl-text-3); }
.ctx-tech { margin-top: 6px; }
.ctx-tech summary { cursor: pointer; font-size: 12px; color: var(--cl-text-3); }
.ctx-tech pre {
  margin: 6px 0 0; padding: 8px 10px; overflow-x: auto; white-space: pre-wrap; word-break: break-all;
  font-family: var(--cl-mono); font-size: 12px; color: var(--cl-text-2);
  background: var(--cl-inset); border-radius: var(--cl-radius-sm);
}
.compact .ctx-body { max-height: 160px; overflow: auto; }
@media (max-width: 620px) { .ctx-block { grid-template-columns: 1fr; gap: 4px; } }
</style>
