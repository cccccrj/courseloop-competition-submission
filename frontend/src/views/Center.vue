<script setup lang="ts">
import { computed, onActivated, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { get } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useCourseStore } from '@/stores/course'
import { useSolveStore } from '@/stores/solve'
import CLBadge from '@/components/ui/CLBadge.vue'
import CLButton from '@/components/ui/CLButton.vue'
import CLCard from '@/components/ui/CLCard.vue'
import CLEmpty from '@/components/ui/CLEmpty.vue'
import CLPageHeader from '@/components/ui/CLPageHeader.vue'
import CLProgress from '@/components/ui/CLProgress.vue'
import CLSkeleton from '@/components/ui/CLSkeleton.vue'
import { coverageText, masteryPct } from '@/utils/mastery'

const router = useRouter()
const auth = useAuthStore()
const courseStore = useCourseStore()
const solve = useSolveStore()
const data = ref<any>(null)
const loading = ref(true)
const assignments = ref<any[]>([])
// 老师下发的作业排在自己的复习任务之前：那是别人交代的事，且有截止日期。
const nextAssignment = computed(() => assignments.value.find((a: any) => !a.completed) || null)

const stateLabel: Record<string, string> = {
  not_started: '未开始', learning: '学习中', fragile: '不稳固', mastered: '已掌握',
  needs_review: '待复习', misconception: '概念错误',
}
const overview = computed(() => data.value?.overview || {})
const nextTask = computed(() => data.value?.due_tasks?.[0] || null)
const nextWeak = computed(() => data.value?.weak_points?.[0] || null)

/* ---------- 主推任务的四要素：做什么 / 为什么 / 约多久 / 做到什么算完 ----------
 * docs/35 P1-5。优先级算法不动（作业 → 到期复习 → 摸底 → 最弱点），只补齐每一支的说明。
 * 用时 = 这个学生每题实际用时的中位数 × 还差的题数；没有作答记录就不显示，不拿默认值冒充。 */
const WEAK_TARGET = 3
function estimate(items: number): number | null {
  const per = data.value?.minutes_per_item
  if (!per || items <= 0) return null
  return Math.max(1, Math.round(per * items))
}
const focus = computed(() => {
  const a = nextAssignment.value
  if (a) {
    const left = (a.points || []).reduce((n: number, p: any) => n + Math.max(0, a.target - (p.done || 0)), 0)
    return {
      why: a.source_name ? `班里「${a.source_name}」出现得多，老师针对它布置了这份作业`
        : '老师布置的作业，有截止日期，排在自己的复习之前',
      minutes: estimate(left),
      done: `${a.points.length} 个知识点各做对 ${a.target} 题（还差 ${left} 题）`,
    }
  }
  const t = nextTask.value
  if (t) {
    return {
      why: t.reason,
      minutes: t.minutes || estimate(t.target_count - (t.completed_count || 0)),
      done: `做对 ${t.target_count} 题，判题后自动更新复习计划`,
    }
  }
  if (data.value?.cold_start) {
    return { why: '还没有作答记录，推荐和复习计划都缺依据', minutes: null,
             done: '做完一轮自适应客观题，系统给测到的知识点写上掌握度初值' }
  }
  const w = nextWeak.value
  if (w) {
    return { why: `掌握度 ${Math.round((w.mastery || 0) * 100)}%，是这门课里目前最弱的知识点`,
             minutes: estimate(WEAK_TARGET), done: `做对 ${WEAK_TARGET} 题` }
  }
  return null
})

// 解题做到一半的会话：首页给一个"继续上次"，不让学生自己去找
const unfinished = computed(() => {
  const sess = solve.session(courseStore.current)
  return sess.sessionId && !sess.resolved && sess.problem ? sess : null
})
const now = new Date()
const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`

async function load() {
  loading.value = true
  const course = encodeURIComponent(courseStore.current)
  try {
    const [center, asg] = await Promise.all([
      get<any>('/learning/center?course=' + course),
      get<{ items: any[] }>('/assignments?course=' + course).catch(() => ({ items: [] })),
    ])
    data.value = center
    assignments.value = asg.items || []
  } finally {
    loading.value = false
  }
}

function practiceAssignment(a: any) {
  const kid = a.points?.[0]?.knowledge_id
  router.push({ name: 'practice', query: { course: courseStore.current, ...(kid ? { kid } : {}) } })
}

function practiceTask(t: any) {
  router.push({ name: 'practice', query: { course: courseStore.current, kid: t.knowledge_id, task: t.id } })
}
function practicePoint(p: any) {
  router.push({ name: 'practice', query: { course: courseStore.current, kid: p.knowledge_id } })
}
function shortTime(value: string) {
  if (!value) return ''
  return value.replace('T', ' ').slice(5, 16)
}

onActivated(async () => {
  await courseStore.load()
  await Promise.all([load(), solve.restore(courseStore.current)])
})
watch(() => courseStore.current, load)
</script>

<template>
  <div class="center">
    <CLPageHeader :title="`${auth.displayName}，继续今天的学习`" sub="按你的作答、错题和复习计划排出今天的任务">
      <template #actions>
        <CLBadge variant="cite">{{ courseStore.info?.short }}</CLBadge>
        <CLBadge v-if="overview.streak_days" variant="ok">连续学习 {{ overview.streak_days }} 天</CLBadge>
      </template>
    </CLPageHeader>

    <CLSkeleton v-if="loading" :rows="6" />
    <template v-else-if="data">
      <CLCard class="focus" :class="{ cold: data.cold_start }">
        <!-- 局部光晕。放在卡片内、绝对定位到左下角，让"今日要做的这一件事"
             自带一点光源，而不是靠加大字号去抢注意力。 -->
        <span class="focus-glow" aria-hidden="true"></span>
        <div class="focus-copy">
          <div class="eyebrow">
            <span class="eyebrow-dot" aria-hidden="true"></span>
            {{ nextAssignment ? '老师布置的作业'
               : nextTask ? '今日优先任务'
               : (data.cold_start ? '从摸底开始' : '下一步建议') }}
          </div>
          <template v-if="nextAssignment">
            <h2>{{ nextAssignment.title }}</h2>
            <p>
              {{ nextAssignment.points.map((p: any) => p.name).join('、') }}：
              每个知识点做对 {{ nextAssignment.target }} 题，已达标
              {{ nextAssignment.points_done }}/{{ nextAssignment.points_total }} 个知识点。
              <template v-if="nextAssignment.due_date">截止 {{ nextAssignment.due_date }}。</template>
            </p>
          </template>
          <template v-else-if="nextTask">
            <h2>{{ nextTask.name }}</h2>
            <p>{{ nextTask.task_type || '复习任务' }}</p>
          </template>
          <!-- 冷启动时不推知识点：还没有作答，排在最前的那个点只是大纲顺序的第一个，
               推它等于随便挑一个。这种情况下唯一有意义的下一步是去摸底。 -->
          <template v-else-if="data.cold_start">
            <h2>先做一次入学摸底</h2>
            <p>十来道客观题自适应定级，测完给测到的知识点写上掌握度初值，
               推荐、复习计划和薄弱排序立刻就有依据了。</p>
          </template>
          <template v-else-if="nextWeak">
            <h2>{{ nextWeak.name }}</h2>
            <p>针对性练习这个知识点。</p>
          </template>
          <template v-else>
            <h2>开始建立你的学习画像</h2><p>选择一门课程并完成第一道练习。</p>
          </template>
          <dl v-if="focus" class="focus-meta">
            <div><dt>为什么</dt><dd>{{ focus.why }}</dd></div>
            <div v-if="focus.minutes"><dt>约多久</dt><dd>{{ focus.minutes }} 分钟<small>（按你最近的做题速度估）</small></dd></div>
            <div><dt>做到什么算完</dt><dd>{{ focus.done }}</dd></div>
          </dl>
        </div>
        <CLButton v-if="nextAssignment" @click="practiceAssignment(nextAssignment)">去做作业</CLButton>
        <CLButton v-else-if="nextTask" @click="practiceTask(nextTask)">开始今日任务</CLButton>
        <CLButton v-else-if="data.cold_start" @click="router.push({ name: 'placement' })">开始入学摸底</CLButton>
        <CLButton v-else-if="nextWeak" @click="practicePoint(nextWeak)">开始练习</CLButton>
        <CLButton v-else @click="router.push({ name: 'practice' })">进入练习</CLButton>
      </CLCard>

      <button v-if="unfinished" class="resume" @click="router.push({ name: 'solve' })">
        <span class="resume-tag">继续上次</span>
        <span class="resume-text">{{ unfinished.originProblem ? '变式题：' : '' }}{{ unfinished.problem }}</span>
        <span class="resume-go">回到解题纠错 →</span>
      </button>

      <div class="grid">
        <CLCard v-if="assignments.length" title="老师下发的作业" sub="做对题就算完成，不需要额外提交">
          <ul class="task-list">
            <li v-for="a in assignments" :key="a.id">
              <button class="row-button" @click="practiceAssignment(a)">
                <span class="row-main">
                  <b>{{ a.title }}</b>
                  <!-- 逐点写出还差多少：只说"还差 2 题"的话，学生会挑最顺手的那个点去做，
                       而那个点多半已经达标了 -->
                  <small>{{ a.points.map((p: any) => `${p.name} ${p.done}/${a.target}`).join(' · ') }}</small>
                </span>
                <CLBadge v-if="a.completed" variant="ok">已完成</CLBadge>
                <CLBadge v-else-if="a.overdue" variant="err">已逾期</CLBadge>
                <CLBadge v-else-if="a.due_date" variant="warn">{{ a.due_date }}</CLBadge>
                <CLBadge v-else variant="cite">进行中</CLBadge>
              </button>
            </li>
          </ul>
        </CLCard>

        <CLCard title="今日复习任务" sub="到期与逾期任务优先显示">
          <template #actions><CLButton variant="text" @click="router.push({ name: 'review' })">查看计划</CLButton></template>
          <CLEmpty v-if="!data.due_tasks.length" title="今天没有到期任务" :hint="data.plan ? '可以提前练习薄弱知识点' : '创建复习计划后会在这里出现每日任务'">
            <CLButton v-if="data.plan && nextWeak" variant="ghost" @click="practicePoint(nextWeak)">练一个薄弱点</CLButton>
            <CLButton v-else-if="!data.plan" variant="ghost" @click="router.push({ name: 'review' })">去创建复习计划</CLButton>
          </CLEmpty>
          <ul v-else class="task-list">
            <li v-for="t in data.due_tasks" :key="t.id">
              <button class="row-button" @click="practiceTask(t)">
                <span class="row-main"><b>{{ t.name }}</b><small>{{ t.task_type }} · {{ t.completed_count }}/{{ t.target_count }} 题<template v-if="t.minutes"> · 约 {{ t.minutes }} 分钟</template></small></span>
                <CLBadge :variant="t.task_date < today ? 'warn' : 'cite'">{{ t.task_date }}</CLBadge>
              </button>
            </li>
          </ul>
        </CLCard>

        <CLCard title="薄弱知识点" sub="错题、掌握度和遗忘时间综合排序">
          <template #actions><CLButton variant="text" @click="router.push({ name: 'practice' })">为我推荐</CLButton></template>
          <ul class="mastery-list">
            <li v-for="p in data.weak_points" :key="p.knowledge_id" @click="practicePoint(p)">
              <CLProgress :value="(p.mastery || 0) * 100" :label="p.name" :right="stateLabel[p.state] || p.state" :tone="p.state === 'misconception' ? 'err' : 'warn'" />
              <div class="point-meta"><span>{{ p.attempts }} 次作答</span><span v-if="p.error_freq">{{ p.error_freq }} 条待解决错题</span><span>{{ p.bank_count }} 道可练</span></div>
            </li>
          </ul>
        </CLCard>

        <CLCard title="最近学习" sub="当前课程最近的判题记录">
          <template #actions><CLButton variant="text" @click="router.push({ name: 'records' })">学习记录</CLButton></template>
          <CLEmpty v-if="!overview.recent_attempts?.length" title="还没有作答记录" hint="完成练习后会在这里留下学习轨迹" />
          <ul v-else class="recent-list">
            <li v-for="a in overview.recent_attempts" :key="a.id">
              <CLBadge :variant="a.score >= 60 ? 'ok' : 'err'">{{ a.score >= 60 ? '答对' : '答错' }}</CLBadge>
              <span class="recent-stem">{{ a.stem }}</span>
              <time>{{ shortTime(a.created_at) }}</time>
            </li>
          </ul>
        </CLCard>

        <CLCard title="我的学习档案" sub="账户与当前课程累计数据">
          <!-- 四个统计数原在主推卡上面，首屏第一眼是数字而不是任务（docs/34 §3.1）。
               降到档案卡里：想看的人看得到，但不再和"今天做什么"抢注意力。 -->
          <div class="mini-stats">
            <div :title="coverageText({ practiced: overview.practiced_points || 0, total: overview.total_points || 0 })">
              <b>{{ masteryPct(overview.average_mastery) }}</b>
              <span>平均掌握度（已练 {{ overview.practiced_points || 0 }}/{{ overview.total_points || 0 }}）</span>
            </div>
            <div><b>{{ overview.today_attempts || 0 }}</b><span>今日完成</span></div>
            <div><b>{{ overview.open_errors || 0 }}</b><span>待解决错题</span></div>
            <div><b>{{ overview.mastered_points || 0 }}/{{ overview.total_points || 0 }}</b><span>已掌握</span></div>
          </div>
          <div class="profile">
            <div class="big-avatar"><img v-if="auth.user?.avatar" :src="auth.user.avatar" alt="" /><span v-else>{{ auth.displayName.slice(0, 1) }}</span></div>
            <div class="profile-id"><b>{{ auth.displayName }}</b><p>{{ auth.user?.student_id || auth.user?.username }}</p></div>
          </div>
          <dl class="profile-grid">
            <div><dt>学院</dt><dd>{{ auth.user?.college || '未填写' }}</dd></div>
            <div><dt>专业</dt><dd>{{ auth.user?.major || '未填写' }}</dd></div>
            <div><dt>累计作答</dt><dd>{{ overview.attempts || 0 }} 题</dd></div>
            <div><dt>正确率</dt><dd>{{ Math.round((overview.correct_rate || 0) * 100) }}%</dd></div>
          </dl>
        </CLCard>
      </div>
    </template>
  </div>
</template>

<style scoped>
.center { max-width: 1120px; margin: 0 auto; }

/* ---------- 分段入场 ---------- */
/* 三块依次浮起，而不是整页一起出现。延迟控制在 .16s 内 ——
   这是每次进站都会看到的页面，动效再"好看"也不能让人等。
   注意本页被 keep-alive 缓存：动画只在数据块真正重建时播放（首次加载、切科目），
   来回切页面不会反复重放。 */
.focus, .grid { animation: sec-in .42s cubic-bezier(.22, 1, .36, 1) both; }
.focus-meta { display: grid; gap: 6px; margin: 12px 0 0; }
.focus-meta div { display: grid; grid-template-columns: 88px 1fr; gap: 8px; font-size: 13px; }
.focus-meta dt { color: var(--cl-accent-soft); font-weight: 700; }
.focus-meta dd { margin: 0; color: var(--cl-text-2); }
.focus-meta small { color: var(--cl-text-3); margin-left: 4px; }
.resume {
  width: 100%; display: flex; align-items: center; gap: 12px; margin: -6px 0 18px; padding: 10px 14px;
  border: 1px solid var(--cl-row-border); background: var(--cl-row); border-radius: var(--cl-radius-sm);
  color: inherit; font: inherit; cursor: pointer; text-align: left;
}
.resume:hover { background: var(--cl-row-hover); border-color: var(--cl-row-border-hover); }
.resume-tag { flex: none; font-size: 12px; font-weight: 800; color: var(--cl-accent-soft); }
.resume-text { flex: 1; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; font-size: 13px; color: var(--cl-text-2); }
.resume-go { flex: none; font-size: 12.5px; color: var(--cl-text-3); }
.mini-stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 14px; }
.mini-stats div { display: flex; flex-direction: column; gap: 2px; }
.mini-stats b { font-size: 16px; }
.mini-stats span { font-size: 11.5px; color: var(--cl-text-3); }
.focus { animation-delay: .08s; }
.grid  { animation-delay: .14s; }
@keyframes sec-in {
  from { opacity: 0; transform: translateY(10px); }
  to   { opacity: 1; transform: none; }
}

/* ---------- 主推卡 ---------- */
/* 整页唯一的"下一步做什么"，要比其它卡片重一档。
   深色下不能靠"底色更浅"来强调（玻璃本来就半透明，提亮只会变灰），
   改成加深靛蓝的卡面 + 左下角一团青色局部光。 */
/* 选择器写成 .focus.clcard 而不是 .focus，是为了压过 CLCard 自己的 .clcard。
   两者单写时权重相同（各自带一个 scoped 属性），谁生效取决于样式注入顺序 ——
   当前顺序恰好是对的（父组件导入子组件，子组件样式先注入），但这是构建产物的
   实现细节，不该拿来当依赖。多一个类名把权重拉开，结果就与顺序无关了。
   下面的 .focus.cold 权重与此相同，靠源码顺序在后覆盖，是刻意的。 */
.focus.clcard {
  position: relative; overflow: hidden;
  display: flex; align-items: center; justify-content: space-between; gap: 24px; margin-bottom: 18px;
  background: var(--cl-accent-surface);
  border-color: var(--cl-tint-border);
}
/* 用固定尺寸而不是百分比：百分比的 bottom 按卡片高度解算，
   而这张卡的高度随文案行数变（一行/两行），光斑会跟着上下跳。 */
.focus-glow {
  position: absolute; left: -60px; top: 50%;
  width: 320px; height: 320px; margin-top: -160px;
  border-radius: 50%;
  background: var(--cl-accent-halo);
  filter: blur(26px); pointer-events: none;
}
/* 冷启动是另一种状态而不是同一张卡换个色：此时没有任务可推，
   文案是"先做几道摸底题"，用琥珀色标记"数据还不够"，与常态的靛蓝区分开。 */
.focus.cold {
  background: var(--cl-accent-surface-warn);
  border-color: var(--cl-accent-surface-warn-border);
}
.focus.cold .focus-glow { background: var(--cl-accent-halo-warn); }
.focus.cold .eyebrow { color: var(--cl-warn-fg); }
.focus.cold .eyebrow-dot { background: var(--cl-warn); box-shadow: var(--cl-dot-glow-warn); }

.focus-copy { position: relative; min-width: 0; }
.eyebrow { display: flex; align-items: center; gap: 7px; color: var(--cl-accent-soft); font-size: 12px; font-weight: 800; letter-spacing: .08em; }
.eyebrow-dot {
  width: 6px; height: 6px; border-radius: 50%; flex: none;
  background: var(--cl-accent); box-shadow: var(--cl-dot-glow);
}
.focus h2 { margin: 7px 0 5px; font-size: 22px; letter-spacing: .2px; }
.focus p { margin: 0; color: var(--cl-text-2); }
.focus :deep(.clb) { position: relative; flex: none; }

/* 两栏瀑布流而不是网格：四张卡的高度随数据差很多（作业有时两行、有时没有），
   网格按行对齐会在矮卡下面留一块空洞（视觉走查）。columns 让卡片自然往上补位。 */
.grid { columns: 2; column-gap: 18px; }
.grid > * { break-inside: avoid; margin-bottom: 18px; }
.task-list, .mastery-list, .recent-list { list-style: none; padding: 0; margin: 0; }
.task-list { display: flex; flex-direction: column; gap: 8px; }

/* ---------- 玻璃行 ---------- */
/* 行在卡片**里面**，不能再套一层 --cl-glass：两层半透明渐变叠起来会闷成一团，
   卡片和行的边界反而消失。用 --cl-row 这种极淡的提亮色，靠"比卡面亮一点"分层。 */
.row-button {
  width: 100%; border: 1px solid var(--cl-row-border); background: var(--cl-row);
  border-radius: var(--cl-radius-sm); padding: 10px 12px;
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  color: inherit; cursor: pointer; text-align: left;
  transition: background .15s ease, border-color .15s ease, transform .15s ease, box-shadow .15s ease;
}
.row-button:hover {
  background: var(--cl-row-hover); border-color: var(--cl-row-border-hover);
  transform: translateX(3px); box-shadow: var(--cl-glow);
}
.row-button:focus-visible { outline: 2px solid var(--cl-brand); outline-offset: 2px; }
.row-main { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.row-main b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row-main small, .point-meta, .profile p { color: var(--cl-text-3); font-size: 12px; }

.mastery-list { display: flex; flex-direction: column; gap: 6px; }
.mastery-list li {
  cursor: pointer; padding: 8px; margin: 0 -8px;
  border-radius: var(--cl-radius-sm);
  border: 1px solid transparent;
  transition: background .15s ease, border-color .15s ease;
}
.mastery-list li:hover { background: var(--cl-row-hover); border-color: var(--cl-row-border-hover); }
.point-meta { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 6px; }

.recent-list { display: flex; flex-direction: column; }
.recent-list li { display: grid; grid-template-columns: auto 1fr auto; gap: 9px; align-items: center; padding: 10px 0; border-bottom: 1px solid var(--cl-row-border); }
.recent-list li:last-child { border-bottom: 0; }
.recent-stem { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; font-size: 13px; color: var(--cl-text-2); }
.recent-list time { color: var(--cl-text-3); font-size: 11.5px; font-family: var(--cl-mono); }

/* ---------- 学习档案 ---------- */
.profile { display: flex; align-items: center; gap: 12px; margin-bottom: 18px; }
.profile-id { min-width: 0; }
/* 头像光环：与侧栏用户卡同一手法，两处一眼能认出是同一个人 */
.big-avatar {
  width: 46px; height: 46px; flex: none; display: grid; place-items: center; border-radius: 50%;
  color: white; font-size: 20px; font-weight: 800;
  background: linear-gradient(135deg, var(--cl-brand), var(--cl-accent));
  box-shadow: 0 0 0 3px var(--cl-tint-border), var(--cl-shadow-md);
}
.big-avatar img { width: 100%; height: 100%; object-fit: cover; border-radius: inherit; }
.profile p { margin: 3px 0 0; }
.profile-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 0; }
.profile-grid div {
  padding: 10px 12px; border-radius: var(--cl-radius-sm);
  background: var(--cl-row); border: 1px solid var(--cl-row-border);
}
.profile-grid dt { font-size: 11.5px; color: var(--cl-text-3); }
.profile-grid dd { margin: 4px 0 0; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

@media (max-width: 820px) { .grid { columns: 2; } }
@media (max-width: 620px) {
  .grid { columns: 1; }
  .mini-stats { grid-template-columns: repeat(2, 1fr); }
  .focus-meta div { grid-template-columns: 1fr; gap: 2px; }
  /* 同样要带 .clcard：align-items 在上面的 .focus.clcard 里设过，
     媒体查询不加权重，单写 .focus 压不过它。 */
  .focus.clcard { align-items: flex-start; flex-direction: column; }
}

@media (prefers-reduced-motion: reduce) {
  .focus, .grid { animation: none; }
  .row-button:hover { transform: none; }
}
</style>
