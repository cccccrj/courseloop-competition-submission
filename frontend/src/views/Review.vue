<script setup lang="ts">
// 复习计划页。核心不是"排了多少"，而是"今天到底要做哪一两件事"——
// 页面结构因此是：今日 → 本周 → 远期里程碑 → 完整排期（默认折叠）。
// 把 90 天的时间轴摊在最上面是上一版的做法，它每天都在提醒你还欠多少。
import { ref, computed, watch, onActivated, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { get, post } from '@/api/client'
import { useCourseStore } from '@/stores/course'
import CLPageHeader from '@/components/ui/CLPageHeader.vue'
import CLButton from '@/components/ui/CLButton.vue'
import CLBadge from '@/components/ui/CLBadge.vue'
import CLCard from '@/components/ui/CLCard.vue'
import CLThinkBlock from '@/components/ui/CLThinkBlock.vue'
import CLEmpty from '@/components/ui/CLEmpty.vue'
import CLSelect from '@/components/ui/CLSelect.vue'
import CLStat from '@/components/ui/CLStat.vue'
import CLWeekBudget, { type Budget } from '@/components/ui/CLWeekBudget.vue'
import MarkdownView from '@/components/MarkdownView.vue'

const courseStore = useCourseStore()
const router = useRouter()
courseStore.load()

function localDate(offset = 0) {
  const d = new Date()
  d.setDate(d.getDate() + offset)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

const WEEKDAYS = ['一', '二', '三', '四', '五', '六', '日']

const budget = ref<Budget>({
  weekly_minutes: [60, 60, 60, 60, 60, 120, 120], intensity: 'medium',
  buffer_weekday: 6, exam_date: '', exceptions: [], is_default: true,
})
// 「不指定」是一个真实选项而不是空占位：下拉一打开就该看到系统默认会怎么排，
// 而不是一个空框让人猜。文案对齐 review_planner 里 `goal or ...` 的兜底措辞，
// 免得界面说的和模型实际收到的是两回事。选中它时提交空串，后端沿用原有默认。
const GOAL_NONE = '不指定（按分值权重均衡）'
const goal = ref(GOAL_NONE)
const plan = ref<any>(null)
const busy = ref(false)
const savingBudget = ref(false)
const showBudget = ref(false)
// 完整排期默认折叠：这一节铺开是十几天的任务，摊在最底下只会把页面拉长，
// 而这一页的重点是"今天做哪一两件"。
// 它原先"没人知道它存在"的问题，由开关本身解决——已经从一行灰色小字改成了
// 横跨整宽的实体按钮（见 .all-toggle），不需要靠默认展开来刷存在感。
const showAll = ref(false)
const showOptional = ref(false)

// 用户**真正保存过**的初试日期。budget.exam_date 在没保存过时会被填一个"120 天后"的
// 默认值供编辑器预填 —— 那个值每天都在往后挪，不能拿来算倒计时。
const savedExam = ref('')

async function loadBudget() {
  try {
    const b: any = await get('/study/budget')
    savedExam.value = b.exam_date || ''
    budget.value = { ...b, exam_date: b.exam_date || localDate(120) }
    // 从没填过预算的人，第一次进来直接把编辑器展开 —— 这一页现在的第一件事
    // 就是"告诉我你有多少时间"，藏在折叠面板里等于没有。
    showBudget.value = !!b.is_default
  } catch { /* 未登录等情况保持默认档，不弹错 */ }
}

async function loadActive() {
  try {
    const result: any = await get('/review/plan/active?course=' + encodeURIComponent(courseStore.current))
    plan.value = result?.plan ?? result
    // review_plans 存了 goal，回显它——否则重进页面看到的永远是"不指定"，
    // 而计划其实是按某个重点科目排的，两者对不上会让人以为设置没生效。
    goal.value = plan.value?.goal || GOAL_NONE
  } catch {
    plan.value = null
  }
}

async function saveBudget() {
  if (savingBudget.value) return
  savingBudget.value = true
  try {
    const saved: any = await post('/study/budget', {
      weekly_minutes: budget.value.weekly_minutes,
      intensity: budget.value.intensity,
      buffer_weekday: budget.value.buffer_weekday,
      exam_date: budget.value.exam_date,
      exceptions: budget.value.exceptions,
    })
    savedExam.value = saved.exam_date || ''
    budget.value = { ...saved, exam_date: saved.exam_date || budget.value.exam_date }
    await generate()
    showBudget.value = false
  } finally {
    savingBudget.value = false
  }
}

async function generate() {
  if (busy.value) return
  busy.value = true
  try {
    plan.value = await post('/review/plan', {
      start_date: localDate(),
      exam_date: budget.value.exam_date,
      goal: goal.value === GOAL_NONE ? '' : goal.value,
      course: courseStore.current,
    })
  } finally {
    busy.value = false
  }
}

function practice(t: any) {
  router.push({ path: '/practice', query: { course: courseStore.current, kid: t.knowledge_id, task: t.id } })
}

async function checkin(t: any) {
  plan.value = await post('/review/checkin', { task_id: t.id })
}

const today = computed(() => plan.value?.today || null)
const week = computed<any[]>(() => plan.value?.week || [])
const milestones = computed<any[]>(() => plan.value?.milestones || [])
const progress = computed(() => plan.value?.progress || {})
const completion = computed(() => Math.round((progress.value.completion_rate || 0) * 100))
// 倒计时与计划进度只认**一个**考试日期（测试报告 I-04）：保存过的初试日期优先，
// 没保存过就用计划自己的。此前倒计时读时间设置、计划进度读计划里存的日期，
// 改过初试日期之后两边各算各的（"距初试 79 天"和"第 38/122 天"对不上）。
const examDate = computed(() => savedExam.value || plan.value?.exam_date || '')
function dayDiff(a: string, b: string) {
  return Math.round((new Date(a + 'T00:00:00').getTime() - new Date(b + 'T00:00:00').getTime()) / 86_400_000)
}
const daysToExam = computed(() => (examDate.value ? Math.max(0, dayDiff(examDate.value, localDate())) : 0))
const coreLeft = computed(() => (today.value?.core || []).filter((t: any) => !t.completed).length)
// 补做列表里也有今天已经补完的（它们仍是"补做"，不会摇身变成必做），计数只算没补完的
const catchupLeft = computed(() => (today.value?.catchup || []).filter((t: any) => !t.completed).length)

// 目标下拉的选项。带上分值是因为"重点选哪门"本质上是在分配时间，而 408 四门的
// 分值差一倍（45 / 45 / 35 / 25），不写出来等于让人凭印象选。
const goalGroups = computed(() => [
  { label: '默认', options: [GOAL_NONE] },
  {
    label: '本轮重点科目',
    options: courseStore.list.map((c) => `以${c.display}为重点（${c.score} 分）`),
  },
])

// 倒计时三态：没填考试日期 / 就是今天 / 还剩 N 天。原先是 v-if="daysToExam"，
// 等于 0 时整块消失 —— 考试当天反而什么都不显示，是最不该沉默的那一天。
const examState = computed<'none' | 'today' | 'left'>(() =>
  !examDate.value ? 'none' : daysToExam.value === 0 ? 'today' : 'left')

// 计划进度：起止日期 + 已进行 + 剩余。剩余就是上面的倒计时（同一个日期、同一个算法），
// 已进行含今天，所以 已进行 + 剩余 = 计划总天数，三个数自洽。
const planDay = computed(() => {
  const start = plan.value?.start_date
  if (!start || !examDate.value) return null
  const md = (d: string) => `${Number(d.slice(5, 7))}/${Number(d.slice(8, 10))}`
  return { from: md(start), to: md(examDate.value),
           elapsed: Math.max(0, dayDiff(localDate(), start) + 1), left: daysToExam.value }
})

// 计划是按旧的初试日期排的：改了日期之后（尤其是在别的科目页面改的，那里只会重排
// 当时那一门）这份计划的任务仍排到旧日期为止。明说出来，给一个重排按钮。
const staleExam = computed(() =>
  !!savedExam.value && !!plan.value?.exam_date && plan.value.exam_date !== savedExam.value
    ? plan.value.exam_date as string : '')

const todayStamp = computed(() => localDate())

// 点周网格里的某一天 → 展开时间设置并聚焦那一天。
// 入口做在网格本身而不是页头按钮上：用户看到的那排格子和他要改的东西是同一个，
// 因果最直接；页头那个 ghost 按钮离面板隔着整个 Hero，点了看不出发生了什么。
const budgetBox = ref<HTMLElement | null>(null)
const focusDay = ref(-1)
async function openBudget(weekday = -1) {
  focusDay.value = weekday
  showBudget.value = true
  await nextTick()
  budgetBox.value?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

// 今天最该做的那一件，给 Hero 底部一行摘要用。
const todayFirst = computed(() => (today.value?.core || []).find((t: any) => !t.completed) || null)

// 柱子高度基准：仍用 budget_minutes（你给的原始时间），周末那根才明显更高。
const weekPeak = computed(() => Math.max(60, ...week.value.map((d: any) => d.budget_minutes || 0)))
function barHeight(v: number) { return Math.round((v / weekPeak.value) * 100) }

// 柱子分四段，都相对"你给的时间"取百分比：
//   其他科目 = budget - cap（408 另外三门的配额）
//   留白     = cap - planned（系统刻意没排的）
//   已排 / 已完成
// 不这么拆的话，空缺段里两种完全不同的东西混成一片灰，用户会读成"计划没做好"。
function seg(d: any, kind: 'others' | 'slack' | 'planned' | 'done') {
  const total = d.budget_minutes || 0
  if (!total) return 0
  const cap = Math.min(d.cap_minutes ?? total, total)
  const planned = Math.min(d.planned_minutes || 0, cap)
  const v = kind === 'others' ? total - cap
    : kind === 'slack' ? cap - planned
    : kind === 'planned' ? planned
    : Math.min(d.done_minutes || 0, planned)
  return Math.round((v / total) * 100)
}

const byDay = computed(() => {
  const g: Record<string, any[]> = {}
  for (const t of plan.value?.tasks || []) {
    if (t.dropped) continue
    ;(g[t.task_date] ||= []).push(t)
  }
  return Object.entries(g).sort(([a], [b]) => a.localeCompare(b))
    .map(([date, tasks]) => ({ date, tasks }))
})

onActivated(async () => { await courseStore.load(); await loadBudget(); await loadActive() })
watch(() => courseStore.current, loadActive)

// 每屏只留一个主按钮（第二阶段 · 全站规范）：今日必做里第一个没完成的任务是主按钮，
// 其余必做、补做、选做一律次级 —— 一列五个同样醒目的「去练习」等于没有重点。
const firstOpenCoreId = computed(() =>
  ((today.value as any)?.core || []).find((t: any) => !t.completed && t.target_count > 0)?.id ?? null)
</script>

<template>
  <div class="wrap">
    <CLPageHeader title="备考节奏" sub="按你的可用时间排，每天只给必做的一两件，落下了自动摊平">
      <template #actions>
        <CLBadge variant="cite">{{ courseStore.info?.short }} · 408 占 {{ courseStore.info?.score }} 分</CLBadge>
      </template>
    </CLPageHeader>

    <!-- Hero：倒计时 + 本周网格。刻意放在 v-if="plan && today" 之外 ——
         倒计时只依赖 budget.exam_date，还没生成计划的人也该一眼看到还剩多少天。 -->
    <section class="hero">
      <div class="hero-count">
        <template v-if="examState === 'left'">
          <div class="hero-eyebrow">距初试还有</div>
          <div class="hero-num">{{ daysToExam }}<span class="hero-unit">天</span></div>
        </template>
        <template v-else-if="examState === 'today'">
          <div class="hero-eyebrow">就是今天</div>
          <div class="hero-num hero-num-sm">初试日</div>
        </template>
        <template v-else>
          <div class="hero-eyebrow">还没填初试日期</div>
          <CLButton variant="ghost" @click="openBudget()">去填写</CLButton>
        </template>
        <div v-if="planDay" class="hero-sub">
          计划 {{ planDay.from }} – {{ planDay.to }} · 已进行 {{ planDay.elapsed }} 天 · 剩 {{ planDay.left }} 天
        </div>
        <div v-if="staleExam" class="hero-stale">
          初试日期已改为 {{ examDate.slice(5).replace('-', '/') }}，这份计划还是按
          {{ staleExam.slice(5).replace('-', '/') }} 排的。
          <button type="button" class="hero-stale-btn" :disabled="busy" @click="generate">按新日期重排</button>
        </div>
      </div>

      <div v-if="week.length" class="hero-week">
        <div class="week-head">
          <span class="week-title">本周安排</span>
          <span class="week-hint">点任意一天可直接改那天</span>
        </div>
        <div class="week">
          <button v-for="d in week" :key="d.date" type="button"
                  :class="['wday', { rest: d.is_buffer_day, off: !d.budget_minutes, now: d.date === todayStamp }]"
                  :title="`点这里改周${WEEKDAYS[d.weekday]}的时间`"
                  @click="openBudget(d.weekday)">
            <div class="bars">
              <!-- 休息日不画柱子：画成柱子会和"我没填时间"长得一样，
                   而这天是用户主动选的，视觉上必须区分开。 -->
              <div v-if="d.is_buffer_day" class="rest-slot" title="今天不派新任务；落下的会挤一点时间在这天补">
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M20 14.5A8 8 0 0 1 9.5 4a8.2 8.2 0 1 0 10.5 10.5z" />
                </svg>
                <span v-if="d.catchup_count" class="rest-tag">补</span>
              </div>
              <div v-else class="bar-track" :style="{ height: barHeight(d.budget_minutes) + '%' }">
                <div class="bar-others" :style="{ height: seg(d, 'others') + '%' }"
                     :title="`其他科目 ${d.budget_minutes - d.cap_minutes} 分钟`"></div>
                <div class="bar-slack" :style="{ height: seg(d, 'slack') + '%' }"
                     :title="`刻意留白 ${d.cap_minutes - d.planned_minutes} 分钟`"></div>
                <div class="bar-fill" :style="{ height: seg(d, 'planned') + '%' }">
                  <div class="bar-done"
                       :style="{ height: d.planned_minutes ? Math.round((d.done_minutes / d.planned_minutes) * 100) + '%' : '0%' }"></div>
                </div>
              </div>
            </div>
            <div class="wday-name">{{ WEEKDAYS[d.weekday] }}</div>
            <div class="wday-min">
              <template v-if="d.is_buffer_day">休息</template>
              <template v-else-if="d.is_exception">例外</template>
              <template v-else-if="!d.budget_minutes">—</template>
              <template v-else>{{ d.planned_minutes }} 分钟</template>
            </div>
            <!-- 必做 / 补做分开写，与下面今日卡片同一个分法；休息日只会有补做 -->
            <div class="wday-core">
              <template v-if="d.core_count">必做 {{ d.core_count }}</template>
              <template v-if="d.core_count && d.catchup_count"> · </template>
              <template v-if="d.catchup_count">补做 {{ d.catchup_count }}</template>
            </div>
          </button>
        </div>

        <div class="week-legend">
          <span><i class="lg lg-fill"></i>已排</span>
          <span><i class="lg lg-done"></i>已完成</span>
          <span><i class="lg lg-slack"></i>留白 · 给意外和不想学的时候</span>
          <span><i class="lg lg-others"></i>其他科目</span>
        </div>

        <div v-if="todayFirst" class="week-next">
          今天必做：<b>{{ todayFirst.name }}</b>
          <template v-if="todayFirst.target_count"> · {{ todayFirst.target_count }} 题</template>
          · 约 {{ todayFirst.minutes }} 分
        </div>
      </div>

      <!-- 时间设置的主入口。原先在页头 #actions 里是个 ghost 按钮，用户反馈
           "右上角不明显" —— 那个位置离面板隔着整个 Hero，权重也最低。
           挪进框内、横跨整宽、给实体描边，点了就在正下方展开。 -->
      <button type="button" class="hero-edit" @click="showBudget ? (showBudget = false) : openBudget()">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="12" cy="12" r="8.4" />
          <path d="M12 7.6V12l2.8 1.8" />
        </svg>
        <span>{{ showBudget ? '收起复习计划设置' : '设置我的复习计划' }}</span>
        <svg class="toggle-caret" :class="{ open: showBudget }" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>
    </section>

    <!-- 展开要有动画。原先是裸 v-if，面板瞬间出现在 Hero 下方，
         用户点完按钮往往察觉不到页面多了一块。 -->
    <Transition name="budget">
      <div v-if="showBudget" ref="budgetBox">
        <CLCard pad class="budget-card"
                title="我的复习计划" sub="全局设置，四门课按 408 分值分摊；改完会立刻重排">
          <CLWeekBudget v-model="budget" :course="courseStore.current" :busy="savingBudget"
                        :focus-day="focusDay" @save="saveBudget" />
          <div class="goal">
            <CLSelect v-model="goal" label="学习目标(可选)" :groups="goalGroups"
                      :allow-create="false" :placeholder="GOAL_NONE" />
          </div>
        </CLCard>
      </div>
    </Transition>

    <CLThinkBlock v-if="busy" class="tb" :streaming="true" label="规划" />

    <template v-if="plan && today">
      <!-- ① 今日 —— 这一页唯一需要立刻做决定的地方 -->
      <CLCard class="focus" :class="{ done: today.done }">
        <span class="focus-glow" aria-hidden="true"></span>
        <div class="focus-head">
          <div>
            <div class="eyebrow">
              <span class="eyebrow-dot" aria-hidden="true"></span>
              {{ today.is_buffer_day ? '今天是休息日' : '今日' }}
            </div>
            <h2 v-if="today.done">今天的必做已经做完了</h2>
            <h2 v-else-if="coreLeft">还有 {{ coreLeft }} 项必做</h2>
            <h2 v-else-if="today.is_buffer_day">今天不排新任务</h2>
            <h2 v-else>今天恰好没有必做任务</h2>
            <p>
              可用 {{ today.budget_minutes }} 分钟 · 已排 {{ today.planned_minutes }} 分钟
              <template v-if="catchupLeft"> · 待补 {{ catchupLeft }} 项</template>
              <template v-if="today.is_buffer_day"> · 今天是休息日，只补之前落下的</template>
            </p>
          </div>
        </div>

        <div v-if="today.core.length" class="tasks">
          <div v-for="t in today.core" :key="t.id" :class="['task', { done: t.completed }]">
            <div class="task-main">
              <span class="task-name">{{ t.name }}</span>
              <CLBadge variant="cite">{{ t.task_type }}</CLBadge>
              <CLBadge v-if="t.completed" variant="ok">已完成</CLBadge>
              <span class="task-min">{{ t.minutes }} 分 · {{ t.completed_count || 0 }}/{{ t.target_count }} 题</span>
              <span v-if="t.due_to" class="task-due">可做到 {{ t.due_to.slice(5) }}</span>
            </div>
            <div class="task-reason"><MarkdownView :source="t.reason || ''" /></div>
            <div v-if="!t.completed" class="task-actions">
              <CLButton v-if="t.target_count > 0" :variant="t.id === firstOpenCoreId ? 'primary' : 'ghost'" @click="practice(t)">去练习</CLButton>
              <CLButton v-else variant="ghost" @click="checkin(t)">完成打卡</CLButton>
            </div>
          </div>
        </div>

        <!-- 补做单独成组，且措辞上不叫"逾期"。它带着原本的到期日，
             说明"这本来是那天的事"，但不把它渲染成一笔欠债。 -->
        <div v-if="today.catchup.length" class="catchup">
          <div class="group-label">待补（原定 {{ today.catchup[0].due_from }} 起，什么时候做都行）</div>
          <div class="tasks">
            <div v-for="t in today.catchup" :key="t.id" :class="['task', 'catch', { done: t.completed }]">
              <div class="task-main">
                <span class="task-name">{{ t.name }}</span>
                <CLBadge variant="cite">{{ t.task_type }}</CLBadge>
                <span class="task-min">{{ t.minutes }} 分 · {{ t.completed_count || 0 }}/{{ t.target_count }} 题</span>
                <CLBadge v-if="t.completed" variant="ok">已完成</CLBadge>
                <span v-else-if="t.due_to" class="task-due">可做到 {{ t.due_to.slice(5) }}</span>
              </div>
              <div v-if="!t.completed" class="task-actions">
                <CLButton v-if="t.target_count > 0" variant="ghost" @click="practice(t)">去练习</CLButton>
                <CLButton v-else variant="ghost" @click="checkin(t)">完成打卡</CLButton>
              </div>
            </div>
          </div>
        </div>

        <div v-if="today.optional.length" class="optional">
          <button class="group-toggle" @click="showOptional = !showOptional">
            有余力再做 · {{ today.optional.length }} 项
            <span class="caret" :class="{ open: showOptional }">▾</span>
          </button>
          <div v-if="showOptional" class="tasks">
            <div v-for="t in today.optional" :key="t.id" :class="['task', { done: t.completed }]">
              <div class="task-main">
                <span class="task-name">{{ t.name }}</span>
                <CLBadge variant="cite">{{ t.task_type }}</CLBadge>
                <CLBadge v-if="t.completed" variant="ok">已完成</CLBadge>
                <span class="task-min">{{ t.minutes }} 分</span>
              </div>
              <div v-if="!t.completed" class="task-actions">
                <CLButton v-if="t.target_count > 0" variant="ghost" @click="practice(t)">去练习</CLButton>
                <CLButton v-else variant="ghost" @click="checkin(t)">完成打卡</CLButton>
              </div>
            </div>
          </div>
        </div>
      </CLCard>

      <!-- ② 本周负载 -->

      <!-- ③ 远期里程碑：按周给配额，不假装知道第 87 天上午做什么 -->
      <CLCard v-if="milestones.length" title="再往后" sub="只给到周，具体哪天到时候按你当时的进度排"
              class="milestones">
        <div v-for="m in milestones" :key="m.week_start" class="milestone">
          <div class="ms-when">{{ m.week_start.slice(5) }} ~ {{ m.week_end.slice(5) }}</div>
          <div class="ms-body">
            <div class="ms-chapters">{{ m.chapters.join('、') || '待定' }}</div>
            <div class="ms-meta">{{ m.point_count }} 个知识点 · 约 {{ Math.round(m.minutes / 60 * 10) / 10 }} 小时</div>
          </div>
        </div>
      </CLCard>

      <div class="stats">
        <CLStat :value="completion + '%'" label="任务完成率" accent />
        <CLStat :value="`${progress.completed_tasks || 0}/${progress.total_tasks || 0}`" label="已完成任务" />
        <CLStat :value="progress.open_errors ?? 0" label="待解决错题" />
        <CLStat :value="`${Math.round((progress.mastery_delta || 0) * 100)}%`" label="掌握度变化" />
      </div>

      <CLCard v-if="plan.summary" title="复习建议" class="summary">
        <MarkdownView :source="plan.summary" />
      </CLCard>

      <!-- ④ 完整排期，默认折叠。开关用与 Hero 那个同一形态的实体按钮：这一节是整页的
           "全貌"，而 .group-toggle 是卡片内部小节用的弱开关（如"选做任务"），
           两者权重不该一样。折叠不等于要藏起来——入口显眼、内容按需展开是两件事。
           天数留在标签里而不是换成"收起/展开"文案——它一直有用，而开合状态由箭头表达。 -->
      <div class="all">
        <button type="button" class="all-toggle" @click="showAll = !showAll">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <rect x="3.5" y="4.5" width="17" height="16" rx="2.4" />
            <path d="M3.5 9.5h17M8.5 4.5v-2M15.5 4.5v-2" />
          </svg>
          <span>完整排期 · {{ byDay.length }} 天</span>
          <svg class="toggle-caret" :class="{ open: showAll }" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M6 9l6 6 6-6" />
          </svg>
        </button>
        <div v-if="showAll" class="timeline">
          <div v-for="d in byDay" :key="d.date" class="day">
            <div class="node"><span class="dot"></span><span class="line"></span></div>
            <div class="day-body">
              <div class="day-head">{{ d.date }}</div>
              <div class="tasks">
                <div v-for="t in d.tasks" :key="t.id"
                     :class="['task', { done: t.completed, soft: t.tier === 'optional' }]">
                  <div class="task-main">
                    <span class="task-name">{{ t.name }}</span>
                    <CLBadge variant="cite">{{ t.task_type }}</CLBadge>
                    <CLBadge v-if="t.tier === 'optional'">选做</CLBadge>
                    <CLBadge v-if="t.completed" variant="ok">已完成</CLBadge>
                    <span class="task-min">{{ t.minutes }} 分 · 可做到 {{ t.due_to }}</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </template>

    <CLEmpty v-else-if="!busy" title="还没有备考计划"
             hint="先填每周的可用时间，系统会按你的错题、掌握度和遗忘间隔排计划，不会排满" />
  </div>
</template>

<style scoped>
.wrap { max-width: 960px; margin: 0 auto; }
.budget-card, .tb, .summary, .milestones { margin-bottom: 16px; }
.goal { margin-top: 16px; }
.stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }

/* ---------- 今日卡 ---------- */
/* 与学习中心的主推卡同族：局部光晕 + eyebrow，让"今天这一件事"自带光源，
   而不是靠加大字号去抢注意力。 */
.focus { position: relative; overflow: hidden; margin-bottom: 16px; }
.focus-glow {
  position: absolute; left: -60px; bottom: -80px; width: 260px; height: 260px;
  border-radius: 50%; pointer-events: none;
  background: radial-gradient(circle, var(--cl-tint) 0%, transparent 70%);
}
/* 必做全部完成 = 整卡转绿边光。这是这一页最重要的一个状态：
   做完必做当天就算完成，选做没做完不影响。 */
.focus.done { border-color: var(--cl-ok); box-shadow: var(--cl-edge-glow-ok); }
.focus-head { position: relative; margin-bottom: 14px; }
.eyebrow {
  display: flex; align-items: center; gap: 7px; margin-bottom: 8px;
  font-size: 12px; font-weight: 700; letter-spacing: .4px; color: var(--cl-text-3);
}
.eyebrow-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--cl-accent-soft); box-shadow: var(--cl-glow-cyan);
}
.focus h2 { margin: 0 0 6px; font-size: 20px; }
.focus p { margin: 0; font-size: 13px; color: var(--cl-text-2); }

.group-label { font-size: 12.5px; font-weight: 700; color: var(--cl-text-3); margin: 16px 0 8px; }
.group-toggle {
  display: flex; align-items: center; gap: 6px; width: 100%;
  border: none; background: transparent; cursor: pointer; font: inherit;
  font-size: 12.5px; font-weight: 700; color: var(--cl-text-2);
  padding: 12px 0 8px;
}
.group-toggle:hover { color: var(--cl-text); }
.caret { transition: transform .15s ease; display: inline-block; }
.caret.open { transform: rotate(180deg); }

/* ---------- 任务行 ---------- */
.tasks { display: flex; flex-direction: column; gap: 10px; }
.task {
  position: relative;
  padding: 12px 14px; border-radius: var(--cl-radius-sm);
  background: var(--cl-row); border: 1px solid var(--cl-row-border);
  transition: border-color .15s ease, opacity .15s ease;
}
.task:hover { background: var(--cl-row-hover); }
.task.done {
  opacity: .74;
  border-left: 3px solid var(--cl-ok); padding-left: 12px;
  box-shadow: var(--cl-edge-glow-ok);
}
/* .74 是全站降得最狠的一档，--cl-text-3 乘下来只剩 3.3:1。
   同 Records：把卡里最弱的一级提到 --cl-text-2，降档观感保留，字重新读得出。 */
.task.done .task-min { color: var(--cl-text-2); }
.task.done:hover { opacity: 1; }
/* 补做用琥珀竖条而不是红色：它是"还没做"，不是"错了"。
   全站的状态标记都是左侧 3px 竖条 + 同色辉光，这里沿用。 */
.task.catch { border-left: 3px solid var(--cl-warn); padding-left: 12px; }
.task.soft { opacity: .82; }
.task-main { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.task-name { font-weight: 700; }
.task-min { font-size: 12.5px; color: var(--cl-text-3); }
.task-reason { font-size: 13px; color: var(--cl-text-2); margin-top: 6px; }
.task-actions { display: flex; justify-content: flex-end; margin-top: 10px; }

/* ---------- Hero：倒计时 + 本周网格 ---------- */
.hero {
  margin-bottom: 16px; padding: 24px 26px 20px;
  border-radius: var(--cl-radius-lg);
  border: 1px solid var(--cl-glass-border);
  background: var(--cl-glass);
  box-shadow: var(--cl-glass-shadow);
  display: grid; grid-template-columns: minmax(180px, 232px) 1fr; gap: 30px; align-items: center;
}
.hero-count { display: flex; flex-direction: column; gap: 4px; align-items: flex-start; }
.hero-eyebrow { font-size: 12.5px; font-weight: 700; letter-spacing: .6px; color: var(--cl-text-3); }
/* 数字**永远**是这一套渐变，不随天数临近变红、不加感叹号。把倒计时做成
   危险色是市面上最常见的反模式：它只制造焦虑，不改变任何一天该做的事。 */
.hero-num {
  font-size: clamp(46px, 7vw, 76px); font-weight: 800; line-height: 1.02;
  letter-spacing: -1.5px;
  background: var(--cl-text-grad); -webkit-background-clip: text; background-clip: text;
  color: transparent;
}
.hero-num-sm { font-size: clamp(28px, 4vw, 40px); letter-spacing: -.5px; }
.hero-unit { font-size: .34em; font-weight: 700; margin-left: 6px; letter-spacing: 0; }
.hero-sub { font-size: 12.5px; color: var(--cl-text-3); margin-top: 2px; }
.hero-stale { margin-top: 8px; font-size: 12px; line-height: 1.5; color: var(--cl-warn-fg); }
.hero-stale-btn {
  margin-left: 4px; padding: 0; border: none; background: none; cursor: pointer;
  font: inherit; font-weight: 700; color: var(--cl-brand-600); text-decoration: underline;
}
.hero-stale-btn:disabled { opacity: .5; cursor: default; }
.hero-week { min-width: 0; }

.week { display: grid; grid-template-columns: repeat(7, 1fr); gap: 8px; align-items: end; }
/* 每一格是个真按钮：点它就去改那天的时间。用 <button> 而不是加 @click 的 div，
   是为了白拿键盘可达与焦点环 —— 这排格子现在是这一页最主要的入口。 */
.wday {
  display: flex; flex-direction: column; align-items: center; gap: 5px;
  padding: 6px 2px 5px; border-radius: var(--cl-radius-sm);
  border: 1px solid transparent;
  background: transparent; font: inherit; color: inherit;
  cursor: pointer; text-align: center;
  transition: background .15s ease, border-color .15s ease, transform .15s ease;
}
.wday:hover { background: var(--cl-row-hover); border-color: var(--cl-row-border-hover); transform: translateY(-2px); }
.wday:focus-visible { outline: 2px solid var(--cl-brand); outline-offset: 2px; }

/* 明写的提示行。只靠 hover 变色的话，不动鼠标的人根本不知道这排能点 —— 
   用户反馈的"展开入口不明显"就是这个问题。 */
.week-head {
  display: flex; align-items: baseline; justify-content: space-between;
  gap: 10px; margin-bottom: 9px;
}
.week-title { font-size: 12.5px; font-weight: 700; color: var(--cl-text-2); letter-spacing: .3px; }
.week-hint { font-size: 11.5px; color: var(--cl-text-3); }

/* 时间设置的主入口：横跨 Hero 整宽的实体按钮。
   grid-column: 1/-1 让它在两列布局下自己占满一行，窄屏单列时也自然成立。 */
/* 页面级折叠开关的统一形态：横跨整宽的实体按钮，tint 底 + 强描边。
   两个用它的地方（Hero 的时间设置、底部的完整排期）都是"点开会多出一整块内容"的
   开关，权重必须一样；抄成两份的话调一处忘一处，两个按钮很快就长得不像一家人。
   卡片内部小节的弱开关走 .group-toggle，不要混用。 */
.hero-edit, .all-toggle {
  display: flex; align-items: center; justify-content: center; gap: 9px;
  width: 100%; padding: 11px 16px;
  border: 1px solid var(--cl-tint-border-strong);
  border-radius: var(--cl-radius-sm);
  background: var(--cl-tint);
  color: var(--cl-brand-700);
  font: inherit; font-size: 13.5px; font-weight: 700; cursor: pointer;
  transition: background .15s ease, border-color .15s ease, transform .15s ease;
}
.hero-edit { grid-column: 1 / -1; margin-top: 4px; }
.all-toggle { margin: 2px 0 12px; }
.hero-edit:hover, .all-toggle:hover {
  background: var(--cl-tint-strong); border-color: var(--cl-brand);
  transform: translateY(-1px);
}
.hero-edit:focus-visible, .all-toggle:focus-visible {
  outline: 2px solid var(--cl-brand); outline-offset: 2px;
}
.hero-edit svg, .all-toggle svg {
  width: 16px; height: 16px; fill: none; stroke: currentColor;
  stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round;
}
/* 箭头比按钮里的主图标小一号。必须写成后代选择器：上面的 `.hero-edit svg` 是
   (0,1,1)，单靠一个类名 (0,1,0) 压不过它，宽高会被 16px 吃掉。 */
.hero-edit .toggle-caret, .all-toggle .toggle-caret {
  width: 14px; height: 14px; transition: transform .22s ease;
}
.toggle-caret.open { transform: rotate(180deg); }
/* 今天：整格挑出来，不靠"多一个小圆点"那种要找才看得见的标记 */
.wday.now { background: var(--cl-tint); border-color: var(--cl-tint-border-strong); }
.bars { height: 136px; width: 100%; display: flex; align-items: flex-end; justify-content: center; }

/* 柱子四段，自上而下：其他科目 / 留白 / 已排（内含已完成）。
   之所以要把"其他科目"单独画出来：轨道高度用的是 budget_minutes，那是四门课
   共用的原始时间，而 planned 只属于当前这门。不拆的话空缺里混着"别的课占的"
   和"系统故意留的"，用户只会读成"我的计划怎么这么空"。 */
.bar-track {
  width: 34px; min-height: 5px; border-radius: 7px;
  background: var(--cl-inset); border: 1px solid var(--cl-glass-border);
  display: flex; flex-direction: column; justify-content: flex-end; overflow: hidden;
}
.bar-others { width: 100%; background: var(--cl-inset-deep); }
/* 留白用斜纹而不是空槽：空着的槽读作"没安排"，有纹理才读得出"这是留给你的" */
.bar-slack {
  width: 100%;
  background: repeating-linear-gradient(135deg,
    var(--cl-tint) 0 3px, transparent 3px 7px);
}
.bar-fill {
  width: 100%; background: var(--cl-brand-grad); box-shadow: var(--cl-glow);
  display: flex; flex-direction: column; justify-content: flex-end;
}
.bar-done { width: 100%; background: var(--cl-ok); }

/* 休息日整格换语言：不画柱子。画成"矮柱子"会和"我没填时间"（.off）撞脸，
   而这天是用户主动选的，得看得出是两回事。 */
.rest-slot {
  width: 34px; height: 100%; border-radius: 7px;
  background: var(--cl-tint); border: 1px dashed var(--cl-tint-border-strong);
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px;
}
.rest-slot svg {
  width: 17px; height: 17px; fill: none;
  stroke: var(--cl-brand-600); stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round;
}
/* 有补做落进这天时才出现：说明"这天本该空着，但帮你补了一点" */
.rest-tag {
  font-size: 9.5px; font-weight: 800; letter-spacing: .3px;
  color: var(--cl-brand-700); background: var(--cl-brand-100);
  padding: 0 4px; border-radius: 4px;
}

/* 宽限日期用中性三级文字色。这里绝不能用 --cl-warn/--cl-err：
   它传达的是"你还有时间"，不是"你快迟到了"。 */
.task-due { font-size: 11px; color: var(--cl-text-3); }
.wday-name { font-size: 12px; font-weight: 700; color: var(--cl-text-2); }
.wday-min { font-size: 11px; color: var(--cl-text-3); }
.wday-core { font-size: 10px; color: var(--cl-text-4); min-height: 13px; }
.wday.rest .wday-min { color: var(--cl-brand-600); font-weight: 700; }
.wday.off .bar-track { opacity: .5; }

.week-legend {
  display: flex; flex-wrap: wrap; gap: 6px 16px; margin-top: 14px;
  font-size: 11px; color: var(--cl-text-3);
}
.week-legend span { display: inline-flex; align-items: center; gap: 5px; }
.lg { width: 9px; height: 9px; border-radius: 3px; display: inline-block; }
.lg-fill { background: var(--cl-brand-grad); }
.lg-done { background: var(--cl-ok); }
.lg-slack { background: repeating-linear-gradient(135deg, var(--cl-tint) 0 2px, transparent 2px 4px);
            border: 1px solid var(--cl-glass-border); }
.lg-others { background: var(--cl-inset-deep); }

/* 展开/收起动画。grid-template-rows 0fr→1fr 是能对"高度未知的内容"做过渡的
   办法，比写死 max-height 准确 —— 后者取小了会裁掉内容，取大了动画会突然一顿。 */
.budget-enter-active, .budget-leave-active {
  display: grid; grid-template-rows: 1fr;
  transition: grid-template-rows .28s ease, opacity .28s ease;
}
.budget-enter-active > div, .budget-leave-active > div { overflow: hidden; }
.budget-enter-from, .budget-leave-to { grid-template-rows: 0fr; opacity: 0; }

.week-next {
  margin-top: 12px; padding-top: 11px; border-top: 1px solid var(--cl-glass-hi);
  font-size: 13px; color: var(--cl-text-2);
}
.week-next b { color: var(--cl-text); font-weight: 700; }

/* ---------- 里程碑 ---------- */
.milestone {
  display: flex; gap: 14px; padding: 11px 0;
  border-bottom: 1px solid var(--cl-row-border);
}
.milestone:last-child { border-bottom: none; }
.ms-when { flex: none; width: 96px; font-size: 12.5px; font-weight: 700; color: var(--cl-brand-700); }
.ms-body { flex: 1; min-width: 0; }
.ms-chapters { font-size: 13.5px; font-weight: 600; }
.ms-meta { font-size: 12.5px; color: var(--cl-text-3); margin-top: 3px; }

/* ---------- 完整排期时间轴 ---------- */
.timeline { display: flex; flex-direction: column; }
.day { display: flex; gap: 16px; }
.node { display: flex; flex-direction: column; align-items: center; width: 16px; flex-shrink: 0; }
/* 圆点是时间轴的节拍器。外环用半透明靛蓝而不是 --cl-brand-100 的实色 ——
   实色环在氛围底上会显出一圈方方正正的色块边缘。 */
.dot {
  width: 12px; height: 12px; border-radius: 50%; margin-top: 5px; flex: none;
  background: var(--cl-accent-soft);
  box-shadow: var(--cl-glow-cyan);
}
/* 轨道两端渐隐：一条从头拉到尾的实线会把整页竖着切开。
   渐变让它在每一段的首尾自然消失，视觉上是"节与节之间的连接"而不是一条边框。 */
.line {
  flex: 1; width: 2px; margin: 6px 0; border-radius: 2px;
  background: linear-gradient(180deg,
    var(--cl-tint-border-strong), var(--cl-tint) 55%, transparent);
}
.day:last-child .line { display: none; }
.day-body { flex: 1; min-width: 0; padding-bottom: 20px; }
.day-head { font-weight: 800; color: var(--cl-brand-700); margin-bottom: 10px; letter-spacing: .3px; }

/* Hero 在窄屏堆成上下两段：倒计时横过来占一行，周网格铺满下面一行。
   两列硬挤的话柱子会窄到读不出四段差别，还不如换排法。 */
@media (max-width: 880px) {
  .hero {
    grid-template-columns: 1fr; gap: 18px; padding: 20px 18px 16px;
  }
  .hero-count { flex-direction: row; align-items: baseline; flex-wrap: wrap; gap: 4px 12px; }
  .hero-sub { margin-top: 0; }
}

@media (max-width: 720px) {
  .stats { grid-template-columns: repeat(2, 1fr); }
  .week { gap: 5px; }
  .bars { height: 104px; }
  .bar-track, .rest-slot { width: 24px; }
  .week-legend { gap: 5px 12px; }
  .ms-when { width: 72px; }
}
</style>
