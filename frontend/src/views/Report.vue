<script setup lang="ts">
/**
 * AI 学情周报。
 *
 * 这页的全部风险在"数字是不是这一周的"。后端 /report/weekly 出的是窗口聚合
 * （见 backend/agents/weekly_report.py），这里只负责别把它渲染回累计量的样子：
 *
 * 1) **三种"空"分开显示**，不合并成一句"暂无数据"：
 *    - 这周没练 → 说这周没记录，并给上一周的入口（多半是想看上周）
 *    - 没有上周基线 → 掌握度变化显示"首次生成"，不显示 0.0%
 *    - 模型没配/调用失败 → 数字照出，只把点评那块换成说明
 * 2) **模型写的和算出来的要能一眼分开**：点评块左侧有一道青色亮条 + "AI 点评"
 *    标签，其余数字块一律没有。截图传出去之后，读的人要能分清哪句是统计、
 *    哪句是模型的话。
 * 3) 分享卡不截 DOM，是手画的 canvas（utils/shareCard.ts 有原因）。
 */
import { computed, onActivated, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { get } from '@/api/client'
import { toast } from '@/api/toast'
import { useAuthStore } from '@/stores/auth'
import { useCourseStore } from '@/stores/course'
import { drawShareCard, downloadDataUrl } from '@/utils/shareCard'
import CLBadge from '@/components/ui/CLBadge.vue'
import CLButton from '@/components/ui/CLButton.vue'
import CLCard from '@/components/ui/CLCard.vue'
import CLEmpty from '@/components/ui/CLEmpty.vue'
import CLPageHeader from '@/components/ui/CLPageHeader.vue'
import CLProgress from '@/components/ui/CLProgress.vue'
import CLSkeleton from '@/components/ui/CLSkeleton.vue'
import CLStat from '@/components/ui/CLStat.vue'

interface Report {
  course_display: string
  week_start: string
  week_end: string
  offset: number
  empty: boolean
  cold_start: boolean
  activity: {
    attempts: number; correct: number; hinted_attempts: number
    duration_minutes: number; active_days: number; correct_rate: number | null
    by_knowledge: { knowledge_id: number; name: string; attempts: number; correct_n: number }[]
  }
  errors: { opened: number; closed: number; error_types: { error_type: string; name: string; cnt: number }[] }
  mastery: {
    average_delta: number | null; has_baseline: boolean; average_now: number
    mastered_points: number; total_points: number
    gained: { knowledge_id: number; name: string; before: number; after: number; delta: number }[]
  }
  weak_points: {
    knowledge_id: number; name: string; chapter: string; mastery: number
    error_freq: number; top_error_name: string; has_misconception: boolean
  }[]
  comment: string
  comment_skipped: string
}

const router = useRouter()
const auth = useAuthStore()
const courseStore = useCourseStore()

const data = ref<Report | null>(null)
const loading = ref(true)
const offset = ref(0)
const exporting = ref(false)

// 服务端把 offset 夹在 [0, 52]；这里用同一个上限，免得按钮还能点但请求已经不动了
const MAX_OFFSET = 52

const weekLabel = computed(() => {
  if (offset.value === 0) return '本周'
  if (offset.value === 1) return '上周'
  return `${offset.value} 周前`
})

const commentNote: Record<string, string> = {
  no_activity: '这一周没有学习记录，暂无点评。',
  llm_unavailable: 'AI 点评暂未开通，上面的统计不受影响。',
  llm_failed: '这周的点评暂时没生成出来，稍后重新生成即可，上面的统计不受影响。',
}

async function load() {
  loading.value = true
  try {
    const c = encodeURIComponent(courseStore.current)
    data.value = await get<Report>(`/report/weekly?course=${c}&offset=${offset.value}`)
  } finally {
    loading.value = false
  }
}

function step(delta: number) {
  const next = Math.max(0, Math.min(MAX_OFFSET, offset.value + delta))
  if (next === offset.value) return
  offset.value = next
  load()
}

function practice(kid: number) {
  router.push({ name: 'practice', query: { course: courseStore.current, kid } })
}

function exportImage() {
  const r = data.value
  if (!r) return
  exporting.value = true
  try {
    const a = r.activity
    const url = drawShareCard({
      courseDisplay: r.course_display,
      weekStart: r.week_start,
      weekEnd: r.week_end,
      who: auth.displayName,
      stats: [
        { label: '完成题目', value: String(a.attempts) },
        { label: '正确率', value: a.correct_rate === null ? '—' : `${Math.round(a.correct_rate * 100)}%` },
        { label: '学习时长', value: `${a.duration_minutes} 分` },
        { label: '学习天数', value: `${a.active_days} 天` },
      ],
      masteryDelta: r.mastery.average_delta,
      errorTypes: r.errors.error_types.slice(0, 5).map((t) => ({ name: t.name, cnt: t.cnt })),
      weakPoints: r.weak_points.slice(0, 5).map((p) => ({ name: p.name, mastery: p.mastery })),
      comment: r.comment,
    })
    downloadDataUrl(url, `CourseLoop-周报-${r.week_start}.png`)
  } catch (e) {
    console.error('weekly report export failed', e)
    toast('导出失败，请稍后重试', 'error')
  } finally {
    exporting.value = false
  }
}

onActivated(async () => { await courseStore.load(); await load() })
watch(() => courseStore.current, () => { offset.value = 0; load() })
</script>

<template>
  <div class="report">
    <CLPageHeader title="学情周报" sub="按自然周汇总你的作答、错题和掌握度变化">
      <template #actions>
        <div class="weeknav">
          <button class="wbtn" :disabled="offset >= MAX_OFFSET" title="更早一周" @click="step(1)">‹</button>
          <span class="wlabel">{{ weekLabel }}</span>
          <button class="wbtn" :disabled="offset <= 0" title="更近一周" @click="step(-1)">›</button>
        </div>
        <CLButton variant="ghost" :disabled="!data || exporting" @click="exportImage">导出图片</CLButton>
      </template>
    </CLPageHeader>

    <CLSkeleton v-if="loading" :rows="7" />
    <template v-else-if="data">
      <div class="range">
        <CLBadge variant="cite">{{ data.course_display }}</CLBadge>
        <span>{{ data.week_start }} 至 {{ data.week_end }}</span>
      </div>

      <!-- 这周什么都没发生：直说，并把入口指向上一周（多半是想看上周） -->
      <CLCard v-if="data.empty" class="blank">
        <CLEmpty :title="`${weekLabel}没有学习记录`"
          hint="没有作答、也没有新增或订正错题。周报只汇总真实发生过的学习，不生成占位内容。">
          <div class="blank-actions">
            <CLButton v-if="offset < MAX_OFFSET" variant="ghost" @click="step(1)">看看更早一周</CLButton>
            <CLButton @click="router.push({ name: 'practice' })">去练几道题</CLButton>
          </div>
        </CLEmpty>
      </CLCard>

      <template v-else>
        <div class="stats">
          <CLStat :value="data.activity.attempts" label="完成题目" accent />
          <CLStat :value="data.activity.correct_rate === null ? '—' : `${Math.round(data.activity.correct_rate * 100)}%`" label="正确率" />
          <CLStat :value="`${data.activity.duration_minutes} 分`" label="学习时长" />
          <CLStat :value="`${data.activity.active_days} 天`" label="学习天数" />
        </div>

        <!-- AI 点评。左边那道亮条 + 角标是这块与全页其它块的唯一区别，
             它标记的是"这段话是模型写的，上面的数字不是"。 -->
        <CLCard class="comment" :class="{ muted: !data.comment }">
          <span class="comment-bar" aria-hidden="true"></span>
          <div class="comment-head">
            <span class="dot" aria-hidden="true"></span>AI 点评
            <CLBadge v-if="!data.comment" variant="warn">未生成</CLBadge>
          </div>
          <p v-if="data.comment" class="comment-text">{{ data.comment }}</p>
          <p v-else class="comment-note">{{ commentNote[data.comment_skipped] || '本周点评暂不可用。' }}</p>
        </CLCard>

        <div class="grid">
          <CLCard title="掌握度变化" sub="与上周同一时刻的快照相比">
            <div v-if="!data.mastery.has_baseline" class="baseline-none">
              <b>首次生成，暂无上周基线</b>
              <p>掌握度的历史快照从本周开始记录，下周这里就能显示涨跌了。
                 现在显示 0% 只会被读成「一周没有进步」，那不是事实。</p>
            </div>
            <template v-else>
              <div class="delta" :class="{ up: (data.mastery.average_delta || 0) > 0, down: (data.mastery.average_delta || 0) < 0 }">
                {{ (data.mastery.average_delta || 0) > 0 ? '+' : '' }}{{ ((data.mastery.average_delta || 0) * 100).toFixed(1) }}%
                <small>全门平均 · 当前 {{ Math.round(data.mastery.average_now * 100) }}%</small>
              </div>
              <CLEmpty v-if="!data.mastery.gained.length" title="本周没有知识点掌握度上升"
                hint="做对题目并通过判题后，掌握度才会更新" />
              <ul v-else class="gain-list">
                <li v-for="g in data.mastery.gained" :key="g.knowledge_id">
                  <span class="g-name">{{ g.name }}</span>
                  <span class="g-move">
                    <i>{{ Math.round(g.before * 100) }}%</i> →
                    <b>{{ Math.round(g.after * 100) }}%</b>
                  </span>
                </li>
              </ul>
            </template>
          </CLCard>

          <CLCard title="错题进出" sub="本周新增与订正销账">
            <div class="errflow">
              <div class="ef open"><b>{{ data.errors.opened }}</b><span>新增错题</span></div>
              <div class="ef closed"><b>{{ data.errors.closed }}</b><span>订正销账</span></div>
            </div>
            <CLEmpty v-if="!data.errors.error_types.length" title="本周没有新增错题"
              hint="错因分布只统计本周新记的错题" />
            <ul v-else class="err-list">
              <li v-for="t in data.errors.error_types" :key="t.error_type">
                <CLProgress
                  :value="(t.cnt / data.errors.error_types[0].cnt) * 100"
                  :label="t.name" :right="`${t.cnt} 次`" tone="warn" />
              </li>
            </ul>
          </CLCard>

          <CLCard title="下周优先补这几个" sub="错题、掌握度与遗忘时间综合排序，与练习页同一份">
            <template #actions>
              <CLButton variant="text" @click="router.push({ name: 'map' })">看知识图谱</CLButton>
            </template>
            <CLEmpty v-if="!data.weak_points.length" title="还没有足够数据排序"
              hint="完成几道练习后，这里会给出下周的优先级" />
            <ul v-else class="weak-list">
              <li v-for="p in data.weak_points" :key="p.knowledge_id" @click="practice(p.knowledge_id)">
                <div class="w-top">
                  <b>{{ p.name }}</b>
                  <CLBadge v-if="p.has_misconception" variant="err">概念错误</CLBadge>
                  <CLBadge v-else-if="p.error_freq" variant="warn">{{ p.error_freq }} 道待订正</CLBadge>
                </div>
                <CLProgress :value="p.mastery * 100" :right="`${Math.round(p.mastery * 100)}%`"
                  :tone="p.has_misconception ? 'err' : 'warn'" />
                <small v-if="p.top_error_name">主要错因：{{ p.top_error_name }}</small>
              </li>
            </ul>
          </CLCard>

          <CLCard title="本周练了什么" sub="按作答次数排序">
            <CLEmpty v-if="!data.activity.by_knowledge.length" title="本周没有作答记录"
              hint="错题订正也会计入周报，但不算作答" />
            <ul v-else class="did-list">
              <li v-for="k in data.activity.by_knowledge" :key="k.knowledge_id">
                <span class="d-name">{{ k.name }}</span>
                <span class="d-num">{{ k.correct_n }}/{{ k.attempts }} 对</span>
              </li>
            </ul>
            <p v-if="data.activity.hinted_attempts" class="hint-note">
              其中 {{ data.activity.hinted_attempts }} 次用了提示（用提示做对的题，掌握度涨得少一些）。
            </p>
          </CLCard>
        </div>
      </template>
    </template>
  </div>
</template>

<style scoped>
.report { max-width: 1120px; margin: 0 auto; }

/* ---------- 周切换 ---------- */
.weeknav {
  display: flex; align-items: center; gap: 2px;
  background: var(--cl-row); border: 1px solid var(--cl-row-border);
  border-radius: var(--cl-radius-pill); padding: 3px;
}
.wbtn {
  width: 28px; height: 28px; border: 0; border-radius: 50%;
  background: transparent; color: var(--cl-text-2); cursor: pointer; font-size: 17px; line-height: 1;
  transition: background .15s ease, color .15s ease;
}
.wbtn:hover:not(:disabled) { background: var(--cl-row-hover); color: var(--cl-text); }
.wbtn:disabled { opacity: .32; cursor: default; }
.wlabel { font-size: 13px; font-weight: 700; color: var(--cl-text); min-width: 52px; text-align: center; }

.range { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; font-size: 13px; color: var(--cl-text-3); }

.stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 16px; }
.blank-actions { display: flex; gap: 10px; justify-content: center; }

/* ---------- AI 点评 ---------- */
/* 全页唯一一块"模型写的字"。左边的亮条不是装饰，是与上下确定性数字的分界线；
   截图转发出去之后，读的人只能靠这个区分统计和模型的话。 */
.comment.clcard { position: relative; margin-bottom: 16px; overflow: hidden; }
.comment-bar {
  position: absolute; left: 0; top: 14px; bottom: 14px; width: 3px; border-radius: 0 2px 2px 0;
  background: linear-gradient(180deg, var(--cl-accent), var(--cl-accent-tint));
}
.comment.muted .comment-bar { background: var(--cl-neutral-bd); }
.comment-head {
  display: flex; align-items: center; gap: 8px;
  font-size: 12px; font-weight: 800; letter-spacing: .08em; color: var(--cl-accent-soft);
}
.comment.muted .comment-head { color: var(--cl-text-3); }
.comment-head .dot {
  width: 6px; height: 6px; border-radius: 50%; background: var(--cl-accent);
  box-shadow: var(--cl-dot-glow);
}
.comment.muted .comment-head .dot { background: var(--cl-text-3); box-shadow: none; }
.comment-text { margin: 10px 0 0; font-size: 15px; line-height: 1.85; color: var(--cl-text-2); white-space: pre-wrap; }
.comment-note { margin: 10px 0 0; font-size: 13.5px; line-height: 1.7; color: var(--cl-text-3); }

.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; align-items: start; }

/* ---------- 掌握度变化 ---------- */
.baseline-none b { font-size: 15px; color: var(--cl-text); }
.baseline-none p { margin: 6px 0 0; font-size: 13px; line-height: 1.7; color: var(--cl-text-3); }
.delta {
  font-size: 32px; font-weight: 800; font-variant-numeric: tabular-nums;
  color: var(--cl-text-2); margin-bottom: 14px;
}
.delta.up { color: var(--cl-ok); }
.delta.down { color: var(--cl-err); }
.delta small { display: block; font-size: 12.5px; font-weight: 600; color: var(--cl-text-3); margin-top: 4px; }

.gain-list, .err-list, .weak-list, .did-list { list-style: none; margin: 0; padding: 0; }
.gain-list { display: flex; flex-direction: column; gap: 8px; }
.gain-list li {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  padding: 9px 11px; border-radius: var(--cl-radius-sm);
  background: var(--cl-row); border: 1px solid var(--cl-row-border);
}
.g-name { font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.g-move { font-size: 13px; color: var(--cl-text-3); font-variant-numeric: tabular-nums; flex: none; }
.g-move b { color: var(--cl-ok); }
.g-move i { font-style: normal; }

/* ---------- 错题进出 ---------- */
.errflow { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 14px; }
.ef {
  display: flex; flex-direction: column; gap: 3px; padding: 12px 14px;
  border-radius: var(--cl-radius-sm); background: var(--cl-row);
  border: 1px solid var(--cl-row-border); border-left-width: 3px;
}
.ef b { font-size: 22px; font-variant-numeric: tabular-nums; }
.ef span { font-size: 12px; color: var(--cl-text-3); }
.ef.open { border-left-color: var(--cl-warn); }
.ef.open b { color: var(--cl-warn-fg); }
.ef.closed { border-left-color: var(--cl-ok); }
.ef.closed b { color: var(--cl-ok-fg); }
.err-list { display: flex; flex-direction: column; gap: 10px; }

/* ---------- 薄弱点 / 本周练了什么 ---------- */
.weak-list { display: flex; flex-direction: column; gap: 10px; }
.weak-list li {
  cursor: pointer; padding: 10px 12px; border-radius: var(--cl-radius-sm);
  background: var(--cl-row); border: 1px solid var(--cl-row-border);
  transition: background .15s ease, border-color .15s ease, transform .15s ease;
}
.weak-list li:hover {
  background: var(--cl-row-hover); border-color: var(--cl-row-border-hover); transform: translateX(3px);
}
.w-top { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.w-top b { font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.weak-list small { display: block; margin-top: 6px; font-size: 12px; color: var(--cl-text-3); }

.did-list { display: flex; flex-direction: column; }
.did-list li {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  padding: 9px 0; border-bottom: 1px solid var(--cl-row-border); font-size: 13.5px;
}
.did-list li:last-child { border-bottom: 0; }
.d-name { color: var(--cl-text-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.d-num { color: var(--cl-text-3); font-variant-numeric: tabular-nums; flex: none; }
.hint-note { margin: 12px 0 0; font-size: 12.5px; line-height: 1.7; color: var(--cl-text-3); }

@media (max-width: 820px) { .stats, .grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 620px) { .stats, .grid, .errflow { grid-template-columns: 1fr; } }
@media (prefers-reduced-motion: reduce) { .weak-list li:hover { transform: none; } }
</style>
