<script setup lang="ts">
/**
 * 真题模考：按 408 试卷结构组卷 → 180 分钟限时 → 交卷阅卷 → 出分归因。
 *
 * 这页只有一件事不能出错：**分数**。所以渲染上守三条：
 *
 * 1) **满分是实际满分**。题库组不满整卷时后端给的 full_score 就不是 150，
 *    成绩单必须把"X / 实际满分"放主位，折算到 150 分制的值只能做小字副标，
 *    并写明是折算 —— 60 分卷考 55 折成 137.5，不说清楚就是虚报。
 * 2) **缺口摆在开考前**。缺哪门、缺几道，开考前就要看见，不能考完了才发现
 *    这张卷子只有半张。
 * 3) **倒计时以服务端为准**。剩余时间来自后端（按开考时刻算），本地只做每秒递减；
 *    刷新页面重新拉一次就对齐了 —— 纯前端计时器一刷新就归零，那等于没有限时。
 *
 * 状态本身不在这里，在 stores/exam.ts：卷子、作答、停在第几题、阅卷进度、成绩单
 * 都要活过刷新和切页，而组件只负责画。三种被打断的处境怎么接回来，见那个文件。
 */
import { computed, onActivated, ref } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { toast } from '@/api/toast'
import { useExamStore, type ExamHistoryRow, type ExamItem } from '@/stores/exam'
import CLBadge from '@/components/ui/CLBadge.vue'
import CLCitations from '@/components/ui/CLCitations.vue'
import CLButton from '@/components/ui/CLButton.vue'
import CLCard from '@/components/ui/CLCard.vue'
import CLEmpty from '@/components/ui/CLEmpty.vue'
import CLPageHeader from '@/components/ui/CLPageHeader.vue'
import CLProgress from '@/components/ui/CLProgress.vue'
import CLSkeleton from '@/components/ui/CLSkeleton.vue'
import CLStat from '@/components/ui/CLStat.vue'
import MarkdownView from '@/components/MarkdownView.vue'
import { splitChoiceOptions } from '@/utils/stem'

const router = useRouter()
const store = useExamStore()
const { loading, busy, preview, exam, answers, cursor, remaining, grading, report, reviewItems } =
  storeToRefs(store)
const current = computed<ExamItem | null>(() => store.current)
const answeredCount = computed(() => store.answeredCount)

const clock = computed(() => {
  const s = Math.max(0, remaining.value)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return `${h}:${String(m).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
})
// 最后十分钟变红。不是装饰：限时考试里"还剩多少"必须能余光扫到。
const urgent = computed(() => remaining.value > 0 && remaining.value <= 600)

/* ---------- 作答 ---------- */
const CHOICES = ['A', 'B', 'C', 'D']
const control = computed(() => {
  const it = current.value
  if (!it) return 'text'
  if (it.verification_type === 'unit_test') return 'code'
  if (it.verification_type === 'numeric') {
    if (it.answer_kind === 'choice') return 'choice'
    if (['scalar', 'percent', 'int'].includes(it.answer_kind)) return 'number'
    return 'token'
  }
  return 'text'
})
const answerOf = computed({
  get: () => (current.value ? answers.value[current.value.seq] || '' : ''),
  // 每次改动都进 store：那里节流写本地快照、也节流暂存到后端。原先只在翻题时
  // 暂存一次，综合题写十分钟不翻页是常事，而刷新就发生在那十分钟里。
  set: (v: string) => { if (current.value) store.setAnswer(current.value.seq, v) },
})
const picked = computed(() => answerOf.value.split(',').filter(Boolean))

function toggleChoice(c: string) {
  const it = current.value
  if (!it) return
  if (it.answer_multi) {
    const set = new Set(picked.value)
    if (set.has(c)) set.delete(c)
    else set.add(c)
    answerOf.value = [...set].sort().join(',')
  } else {
    answerOf.value = c
  }
}

const go = (i: number) => store.go(i)
const begin = () => store.begin()
const submit = (auto = false) => store.submit(auto)
const quit = () => store.quit()

/* ---------- 历次模考 ---------- */
// 成绩单一直存在库里，这是它们唯一的列表入口 —— 没有它，考过的那几场等于不存在。
const historyOpen = ref(false)
const historyRows = ref<ExamHistoryRow[]>([])
const historyBusy = ref(false)

async function toggleHistory() {
  historyOpen.value = !historyOpen.value
  if (!historyOpen.value) return
  historyBusy.value = true
  try {
    historyRows.value = await store.history(10)
  } catch {
    toast('取历次模考失败', 'error')
  } finally {
    historyBusy.value = false
  }
}

async function openPast(row: ExamHistoryRow) {
  if (!row.graded) {
    // 没阅完的那场：走同一条续阅卷的路，别让它停在"没有成绩单"上
    await store.resumeGrading({ session_id: row.session_id, submitted_at: row.submitted_at,
                                graded: 0, total: 0 })
  } else {
    await store.openResult(row.session_id)
  }
  historyOpen.value = false
}

const sectionLabel: Record<string, string> = { choice: '单项选择题', comprehensive: '综合应用题' }

function pct(v: number): string {
  return `${Math.round((v || 0) * 100)}%`
}

function when(ts: string): string {
  return (ts || '').replace(/-/g, '/').slice(0, 16)
}

onActivated(() => store.load())
</script>

<template>
  <div class="exam">
    <CLPageHeader title="真题模考"
      :sub="preview?.structure || '按 408 真卷结构组卷，限时作答，交卷自动阅卷'">
      <template #actions>
        <div v-if="exam" class="clock" :class="{ urgent }">
          <span class="clock-dot" aria-hidden="true"></span>{{ clock }}
        </div>
        <!-- 考试中不给这个入口：限时作答的页面上不该有别的东西可点 -->
        <CLButton v-else variant="ghost" @click="toggleHistory">
          {{ historyOpen ? '收起' : '历次模考' }}
        </CLButton>
      </template>
    </CLPageHeader>

    <!-- 历次模考。成绩单从上线起就能反复打开，缺的一直是这个列表入口。 -->
    <CLCard v-if="historyOpen && !exam" class="past" title="历次模考" sub="点开哪一场，就看哪一场的成绩单">
      <CLSkeleton v-if="historyBusy" :rows="3" />
      <CLEmpty v-else-if="!historyRows.length" title="还没有考过"
        hint="交卷之后这里会留下每一场的成绩单，随时点开逐题回顾。" />
      <ul v-else class="past-list">
        <li v-for="row in historyRows" :key="row.session_id">
          <button type="button" class="past-row" @click="openPast(row)">
            <span class="past-when">{{ when(row.submitted_at) }}</span>
            <span v-if="row.state === 'abandoned'" class="past-score muted">已放弃</span>
            <!-- 没阅完的那场也列出来，并且点得动 —— 它在库里是完整的，只差把剩下几题批完 -->
            <span v-else-if="!row.graded" class="past-score muted">没阅完 · 点击接着批</span>
            <span v-else class="past-score"><b>{{ row.score }}</b> / {{ row.full_score }}</span>
            <span v-if="row.graded && row.full_score !== row.standard_score" class="past-scaled">
              折 150 制约 {{ row.scaled_score }}
            </span>
          </button>
        </li>
      </ul>
    </CLCard>

    <CLSkeleton v-if="loading" :rows="6" />

    <!-- ============ 阅卷中 ============ -->
    <CLCard v-else-if="grading" class="grading">
      <h2>正在阅卷</h2>
      <p>客观题当场判分，综合应用题按得分点逐条比对，需要稍等一会儿。</p>
      <CLProgress :value="(grading.done / Math.max(1, grading.total)) * 100"
        :right="`${grading.done}/${grading.total}`" />
      <!-- 阅卷是边批边落的：中途断了，重新进来会从断点接着批，不会重头再来、
           也不会把这一场丢掉。说出来，考生才敢在这一步关页面。 -->
      <p class="thin-note">每批完一题就存一次。中途断网或关掉页面都不要紧，回到这一页会接着批完。</p>
    </CLCard>

    <!-- ============ 考试中 ============ -->
    <template v-else-if="exam">
      <div class="exam-grid">
        <aside class="cl-card pad">
          <div class="pad-head">
            <b>{{ answeredCount }}/{{ exam.items.length }}</b>
            <span>已作答</span>
          </div>
          <template v-for="sec in (['choice', 'comprehensive'] as const)" :key="sec">
            <div v-if="exam.items.some((i) => i.section === sec)" class="pad-section">
              {{ sectionLabel[sec] }}
            </div>
            <div class="pad-grid">
              <button v-for="it in exam.items.filter((x) => x.section === sec)" :key="it.seq"
                :class="['pad-btn', { on: current?.seq === it.seq, done: (answers[it.seq] || '').trim() }]"
                @click="go(exam.items.indexOf(it))">
                {{ it.seq }}
              </button>
            </div>
          </template>
          <div class="pad-actions">
            <CLButton block :disabled="busy" @click="submit(false)">交卷阅卷</CLButton>
            <CLButton block variant="text" @click="quit">放弃本场</CLButton>
          </div>
        </aside>

        <section v-if="current" class="cl-card qbox">
          <div class="q-head">
            <CLBadge variant="cite">第 {{ current.seq }} 题</CLBadge>
            <CLBadge>{{ sectionLabel[current.section] }} · {{ current.score }} 分</CLBadge>
            <CLBadge>{{ current.knowledge }}</CLBadge>
            <span v-if="current.func_name" class="fn">函数名：<code>{{ current.func_name }}</code></span>
          </div>
          <div class="q-stem"><MarkdownView :source="splitChoiceOptions(current.stem)" /></div>

          <div v-if="control === 'choice'" class="choices">
            <button v-for="c in CHOICES" :key="c" type="button"
              :class="['choice', { on: picked.includes(c) }]" @click="toggleChoice(c)">{{ c }}</button>
            <span class="choice-hint">{{ current.answer_multi ? '可多选' : '单选' }}</span>
          </div>
          <textarea v-else-if="control === 'code'" class="cl-textarea code" v-model="answerOf"
            placeholder="在此写出你的 Python 函数实现…" />
          <div v-else-if="control === 'number'" class="num-input">
            <input class="cl-input" type="text" inputmode="decimal" v-model="answerOf" placeholder="填写数值" />
            <span v-if="current.answer_kind === 'percent'" class="suffix">%</span>
            <span v-else-if="current.answer_unit" class="suffix">{{ current.answer_unit }}</span>
          </div>
          <input v-else-if="control === 'token'" class="cl-input mono" type="text" v-model="answerOf"
            placeholder="规范答案，如 11110011 / 192.168.1.0/26" />
          <textarea v-else class="cl-textarea" v-model="answerOf"
            placeholder="分点写出关键结论和依据，按得分点给分" />

          <div class="q-nav">
            <CLButton variant="ghost" :disabled="cursor === 0" @click="go(cursor - 1)">上一题</CLButton>
            <CLButton variant="ghost" :disabled="cursor >= exam.items.length - 1"
              @click="go(cursor + 1)">下一题</CLButton>
            <span class="thin-note">作答边写边暂存，刷新或换设备都会停在这一题。</span>
          </div>
        </section>
      </div>
    </template>

    <!-- ============ 成绩单 ============ -->
    <template v-else-if="report">
      <CLCard class="score-card">
        <span class="score-glow" aria-hidden="true"></span>
        <div class="score-copy">
          <div class="eyebrow"><span class="eyebrow-dot" aria-hidden="true"></span>本场成绩</div>
          <div class="score-main">
            <b>{{ report.score }}</b><i>/ {{ report.full_score }}</i>
          </div>
          <!-- 折算值只能是小字副标，且必须写明是折算 -->
          <p v-if="report.full_score !== report.standard_score" class="scaled">
            按实际满分 {{ report.full_score }} 分计。折算到 150 分制约
            <b>{{ report.scaled_score }}</b> 分（按得分率折算，仅供参考）
          </p>
          <p v-else class="scaled">标准 408 卷面，满分 {{ report.standard_score }} 分。</p>
        </div>
        <div class="score-actions">
          <CLButton @click="begin">再来一场</CLButton>
          <CLButton variant="ghost" @click="router.push({ name: 'records' })">看错题本</CLButton>
          <CLButton variant="text" @click="store.closeResult()">收起成绩单</CLButton>
        </div>
      </CLCard>

      <div class="stats">
        <CLStat :value="`${report.correct}/${report.count}`" label="答对题数" accent />
        <CLStat :value="pct(report.rate)" label="得分率" />
        <CLStat :value="`${report.answered}/${report.count}`" label="作答题数" />
        <CLStat :value="report.weak_chapters[0]?.chapter || '—'" label="最薄弱章节" />
      </div>

      <div class="grid">
        <CLCard title="分段得分" sub="单选与综合各拿了多少">
          <ul class="bar-list">
            <li v-for="(v, k) in report.sections" :key="k">
              <CLProgress :value="v.rate * 100" :label="sectionLabel[k] || k"
                :right="`${v.score}/${v.full}`" :tone="v.rate < 0.6 ? 'warn' : 'brand'" />
            </li>
          </ul>
        </CLCard>

        <CLCard title="分科得分" sub="四门在 408 里的分值权重不同">
          <ul class="bar-list">
            <li v-for="c in report.courses" :key="c.course">
              <CLProgress :value="c.rate * 100" :label="c.short"
                :right="`${c.score}/${c.full}`" :tone="c.rate < 0.6 ? 'warn' : 'brand'" />
            </li>
          </ul>
        </CLCard>

        <CLCard title="最该补的章节" sub="按本场得分率排序，答错的题已进错题本">
          <template #actions>
            <CLButton variant="text" @click="router.push({ name: 'review' })">排进复习计划</CLButton>
          </template>
          <CLEmpty v-if="!report.weak_chapters.length" title="没有可归因的章节"
            hint="题目需要挂到知识点上才能按章统计" />
          <ul class="bar-list" v-else>
            <li v-for="w in report.weak_chapters" :key="w.course + w.chapter">
              <CLProgress :value="w.rate * 100" :label="w.chapter" :right="`${w.score}/${w.full}`"
                :tone="w.rate < 0.4 ? 'err' : 'warn'" />
            </li>
          </ul>
        </CLCard>
      </div>

      <CLCard title="逐题回顾" sub="没作答的题不算错题">
        <ul class="rv-list">
          <li v-for="it in reviewItems" :key="it.seq"
            :class="{ ok: it.passed, bad: !it.passed && !it.skipped, skip: it.skipped }">
            <div class="rv-head">
              <span class="rv-seq">{{ it.seq }}</span>
              <CLBadge :variant="it.passed ? 'ok' : (it.skipped ? 'warn' : 'err')">
                {{ it.skipped ? '未作答' : (it.passed ? '得满分' : '未得满分') }}
              </CLBadge>
              <CLBadge variant="cite">{{ it.knowledge }}</CLBadge>
              <CLBadge v-if="it.is_past_paper" variant="ok">真题</CLBadge>
              <span class="rv-score">{{ it.score }} / {{ it.full }} 分</span>
            </div>
            <div class="rv-stem"><MarkdownView :source="splitChoiceOptions(it.stem)" /></div>
            <div class="rv-ans">
              <span>你的作答：<b>{{ it.your_answer || '（空）' }}</b></span>
              <span v-if="it.reference">参考：<b>{{ it.reference }}</b></span>
            </div>
            <p v-if="it.details" class="rv-detail">{{ it.details }}</p>
            <!-- 答错的题带教材出处：客观题走确定性判定、没有 LLM 讲解，
                 "错在哪一页"是这里唯一能给的解释。 -->
            <CLCitations v-if="it.citations?.length" class="rv-cites" :citations="it.citations" />
          </li>
        </ul>
      </CLCard>
    </template>

    <!-- ============ 开考前 ============ -->
    <template v-else-if="preview">
      <CLCard v-if="!preview.ready" class="notready">
        <CLEmpty title="题库还组不出一张卷"
          :hint="`模考要从四门里各取若干道能归到知识点的题，当前只能凑出 ${preview.full_score / 2} 道左右（至少需要 ${preview.min_items} 道）。`">
          <p class="thin-note">
            题库正在补充中。在此之前，可以先到「个性化练习」按知识点逐点练习。
          </p>
        </CLEmpty>
        <ul class="gap-list">
          <li v-for="g in preview.shortfall" :key="g.course + g.section">
            <b>{{ g.short }}</b> · {{ sectionLabel[g.section] }}：{{ g.got }} / {{ g.want }} 道
          </li>
        </ul>
      </CLCard>

      <template v-else>
        <CLCard class="intro">
          <h2>按真卷结构组一张，限时 {{ preview.duration_minutes }} 分钟</h2>
          <p>
            单选按四门分值权重分配，综合应用题按分值折算题数；交卷后客观题当场判分、
            综合题按得分点给分，答错的题自动进错题本，并按章给出归因。
          </p>
          <p v-if="preview.past_paper_count" class="thin-note">
            本卷含 <b>{{ preview.past_paper_count }}</b> 道校对过的真题（其余为按大纲生成并通过自检的题）。
            同样没做过时优先出真题。
          </p>
          <div class="plan">
            <div v-for="s in preview.sections" :key="s.course" class="plan-col">
              <b>{{ s.short }}</b>
              <span>单选 {{ s.choice_got }}/{{ s.choice_count }}</span>
              <span>综合 {{ s.comprehensive_got }}/{{ s.comprehensive_count }}（{{ s.comprehensive_score }} 分）</span>
            </div>
          </div>
          <p v-if="preview.full_score !== preview.standard_score" class="warn-note">
            题库不足，这张卷子满分 <b>{{ preview.full_score }}</b> 分（标准 {{ preview.standard_score }} 分）。
            缺口：{{ preview.shortfall.map((g) => `${g.short}${sectionLabel[g.section]}缺 ${g.want - g.got} 道`).join('，') }}。
            成绩单会按实际满分算，不会假装考的是整卷。
          </p>
          <div class="intro-actions">
            <CLButton :disabled="busy" @click="begin">{{ busy ? '组卷中…' : '开始模考' }}</CLButton>
            <span class="thin-note">计时从点下这一刻算起，中途关掉页面也在走。</span>
          </div>
        </CLCard>
      </template>
    </template>
  </div>
</template>

<style scoped>
.exam { max-width: 1120px; margin: 0 auto; }
.thin-note { font-size: 12.5px; line-height: 1.7; color: var(--cl-text-3); }
.thin-note code, .fn code { font-family: var(--cl-mono); }

/* ---------- 倒计时 ---------- */
.clock {
  display: flex; align-items: center; gap: 8px;
  padding: 5px 12px; border-radius: var(--cl-radius-pill);
  background: var(--cl-row); border: 1px solid var(--cl-row-border);
  font-family: var(--cl-mono); font-size: 15px; font-weight: 700; font-variant-numeric: tabular-nums;
}
.clock-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--cl-accent); }
/* 最后十分钟：不做闪烁动画（限时里闪烁只会加焦虑，还过不了 reduced-motion），
   换成语义色 + 一圈辉光，余光能扫到就够了。 */
.clock.urgent {
  color: var(--cl-err-fg); border-color: var(--cl-err-bd);
  box-shadow: var(--cl-glow-err);
}
.clock.urgent .clock-dot { background: var(--cl-err); }

/* ---------- 历次模考 ---------- */
.past { margin-bottom: 16px; }
.past-list { display: flex; flex-direction: column; gap: 6px; }
.past-row {
  display: flex; align-items: baseline; gap: 12px; width: 100%;
  padding: 9px 12px; border-radius: var(--cl-radius-sm); cursor: pointer; font: inherit;
  text-align: left; color: var(--cl-text);
  background: var(--cl-row); border: 1px solid var(--cl-row-border);
  transition: background .12s ease, border-color .12s ease;
}
.past-row:hover { border-color: var(--cl-row-border-hover); }
.past-row:focus-visible { outline: 2px solid var(--cl-brand); outline-offset: 2px; }
.past-when { font-family: var(--cl-mono); font-size: 12.5px; color: var(--cl-text-3); }
/* 分数是这一行的主角，等宽数字让几场排在一起能竖着比 */
.past-score { margin-left: auto; font-variant-numeric: tabular-nums; font-size: 13px; }
.past-score b { font-size: 16px; }
.past-score.muted { color: var(--cl-text-3); font-size: 12.5px; }
.past-scaled { font-size: 12px; color: var(--cl-text-3); }

/* ---------- 考试中 ---------- */
.exam-grid { display: grid; grid-template-columns: 240px 1fr; gap: 18px; align-items: start; }
.pad { position: sticky; top: 12px; }
.pad-head { display: flex; align-items: baseline; gap: 6px; margin-bottom: 12px; }
.pad-head b { font-size: 20px; font-variant-numeric: tabular-nums; }
.pad-head span { font-size: 12px; color: var(--cl-text-3); }
.pad-section { margin: 12px 0 6px; font-size: 11.5px; font-weight: 800; letter-spacing: .06em; color: var(--cl-text-3); }
.pad-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 5px; }
/* 题号方块三态：未答（暗）、已答（有底）、当前（渐变实心）。
   已答态必须比 hover 明显 —— 交卷前扫一眼要能数清还剩几题没做。 */
.pad-btn {
  aspect-ratio: 1; border-radius: 6px; cursor: pointer; font: inherit; font-size: 12px;
  border: 1px solid var(--cl-row-border); background: var(--cl-inset); color: var(--cl-text-3);
  transition: background .12s ease, border-color .12s ease, color .12s ease;
}
.pad-btn:hover { border-color: var(--cl-row-border-hover); color: var(--cl-text); }
.pad-btn.done { background: var(--cl-row); border-color: var(--cl-row-border-hover); color: var(--cl-text); }
.pad-btn.on { background: var(--cl-brand-grad); border-color: transparent; color: var(--cl-on-brand); box-shadow: var(--cl-glow); }
.pad-btn:focus-visible { outline: 2px solid var(--cl-brand); outline-offset: 2px; }
.pad-actions { display: flex; flex-direction: column; gap: 8px; margin-top: 16px; }

.qbox { display: flex; flex-direction: column; gap: 14px; }
.q-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.fn { font-size: 13px; color: var(--cl-text-2); }
.fn code { background: var(--cl-code-inline-bg); color: var(--cl-brand-700); padding: 1px 6px; border-radius: 5px; }
.q-stem { line-height: 1.75; }
.q-nav { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.code { font-family: var(--cl-mono); min-height: 180px; font-size: 13px; background: var(--cl-code-bg); }

.choices { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.choice {
  width: 46px; height: 46px; border-radius: var(--cl-radius-sm); cursor: pointer;
  border: 1.5px solid var(--cl-row-border); background: var(--cl-row);
  font: inherit; font-size: 16px; font-weight: 700; color: var(--cl-text-2);
  transition: border-color .12s ease, background .12s ease, color .12s ease, box-shadow .12s ease;
}
.choice:hover { border-color: var(--cl-row-border-hover); background: var(--cl-row-hover); color: var(--cl-text); }
.choice.on { border-color: transparent; color: var(--cl-on-brand); background: var(--cl-brand-grad); box-shadow: var(--cl-glow); }
.choice:focus-visible { outline: 2px solid var(--cl-brand); outline-offset: 2px; }
.choice-hint { font-size: 12.5px; color: var(--cl-text-3); }
.num-input { display: flex; align-items: center; gap: 8px; }
.num-input .cl-input { width: 220px; font-family: var(--cl-mono); }
.num-input .suffix { font-size: 15px; font-weight: 700; color: var(--cl-accent-soft); }
.mono { font-family: var(--cl-mono); }

/* ---------- 阅卷中 ---------- */
.grading h2 { margin: 0 0 6px; font-size: 18px; }
.grading p { margin: 0 0 14px; color: var(--cl-text-3); font-size: 13.5px; }

/* ---------- 成绩单 ---------- */
.score-card.clcard {
  position: relative; overflow: hidden;
  display: flex; align-items: center; justify-content: space-between; gap: 24px; margin-bottom: 16px;
  background: var(--cl-accent-surface);
  border-color: var(--cl-tint-border);
}
.score-glow {
  position: absolute; left: -60px; top: 50%;
  width: 320px; height: 320px; margin-top: -160px; border-radius: 50%;
  background: var(--cl-accent-halo);
  filter: blur(26px); pointer-events: none;
}
.score-copy { position: relative; min-width: 0; }
.eyebrow { display: flex; align-items: center; gap: 7px; color: var(--cl-accent-soft); font-size: 12px; font-weight: 800; letter-spacing: .08em; }
.eyebrow-dot { width: 6px; height: 6px; border-radius: 50%; flex: none; background: var(--cl-accent); box-shadow: var(--cl-dot-glow); }
.score-main { display: flex; align-items: baseline; gap: 8px; margin: 8px 0 4px; font-variant-numeric: tabular-nums; }
.score-main b { font-size: 40px; letter-spacing: -.5px; }
.score-main i { font-style: normal; font-size: 16px; color: var(--cl-text-3); }
.scaled { margin: 0; font-size: 12.5px; line-height: 1.7; color: var(--cl-text-3); max-width: 60ch; }
.scaled b { color: var(--cl-text-2); }
.score-actions { position: relative; display: flex; flex-direction: column; gap: 8px; flex: none; }

.stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 16px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; align-items: start; margin-bottom: 18px; }
.bar-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 12px; }

.rv-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }
.rv-list li {
  padding: 11px 13px; border-radius: var(--cl-radius-sm);
  background: var(--cl-row); border: 1px solid var(--cl-row-border);
  border-left: 3px solid var(--cl-border-strong);
}
.rv-list li.ok { border-left-color: var(--cl-ok); }
.rv-list li.bad { border-left-color: var(--cl-warn); }
.rv-list li.skip { opacity: .66; }
.rv-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.rv-seq { font-family: var(--cl-mono); font-size: 12px; color: var(--cl-text-3); }
.rv-score { margin-left: auto; font-size: 12.5px; color: var(--cl-text-3); font-variant-numeric: tabular-nums; }
.rv-stem { font-size: 13.5px; line-height: 1.6; color: var(--cl-text-2); }
.rv-ans {
  display: flex; gap: 18px; flex-wrap: wrap; margin-top: 9px; padding-top: 9px;
  border-top: 1px dashed var(--cl-row-border); font-size: 13px; color: var(--cl-text-3);
}
.rv-ans b { color: var(--cl-text); font-family: var(--cl-mono); }
.rv-detail { margin: 8px 0 0; font-size: 12.5px; color: var(--cl-text-3); }

/* ---------- 开考前 ---------- */
.intro h2 { margin: 0 0 8px; font-size: 20px; letter-spacing: .2px; }
.intro p { margin: 0; color: var(--cl-text-2); line-height: 1.8; }
.plan { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 16px 0; }
.plan-col {
  display: flex; flex-direction: column; gap: 4px; padding: 11px 13px;
  border-radius: var(--cl-radius-sm); background: var(--cl-row); border: 1px solid var(--cl-row-border);
  font-size: 12.5px; color: var(--cl-text-3);
}
.plan-col b { font-size: 14px; color: var(--cl-text); }
/* 缺口提示用琥珀：这不是错误，是"内容还没备齐"，与判错的红要分开 */
.warn-note {
  margin: 0 0 4px; padding: 10px 12px; font-size: 12.5px; line-height: 1.75;
  border-left: 3px solid var(--cl-warn); border-radius: var(--cl-radius-sm);
  background: var(--cl-inset); color: var(--cl-text-2);
}
.intro-actions {
  display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
  margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--cl-row-border);
}
.gap-list { list-style: none; margin: 14px 0 0; padding: 0; display: flex; flex-direction: column; gap: 6px; font-size: 13px; color: var(--cl-text-3); }

@media (max-width: 900px) {
  .exam-grid { grid-template-columns: 1fr; }
  .pad { position: static; }
  .pad-grid { grid-template-columns: repeat(10, 1fr); }
}
@media (max-width: 820px) { .stats, .grid, .plan { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 620px) {
  .stats, .grid, .plan { grid-template-columns: 1fr; }
  .score-card.clcard { flex-direction: column; align-items: flex-start; }
}
@media (prefers-reduced-motion: reduce) { .choice, .pad-btn { transition: none; } }
</style>
