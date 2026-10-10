<script setup lang="ts">
import { ref, nextTick, computed, onActivated, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { get, post } from '@/api/client'
import { toast } from '@/api/toast'
import { useCourseStore } from '@/stores/course'
import { useSolveStore } from '@/stores/solve'
import CLPageHeader from '@/components/ui/CLPageHeader.vue'
import CLField from '@/components/ui/CLField.vue'
import CLButton from '@/components/ui/CLButton.vue'
import CLBadge from '@/components/ui/CLBadge.vue'
import CLThinkBlock from '@/components/ui/CLThinkBlock.vue'
import MarkdownView from '@/components/MarkdownView.vue'
import CLCitations from '@/components/ui/CLCitations.vue'

const courseStore = useCourseStore()
const solve = useSolveStore()
courseStore.load()

const problemPlaceholder = computed(() =>
  courseStore.codeEnabled
    ? '粘贴一道数据结构与算法题目，例如：用栈判断字符串括号是否合法匹配…'
    : `粘贴一道${courseStore.info?.short || ''}题目，例如计算题/概念分析题，AI 助手会分步引导并给出依据…`,
)

// 会话状态住在 store 里（见 stores/solve.ts）：切页、刷新、甚至后端重启之后，
// 题目、对话流和提示级别都能接回来。
const sess = computed(() => solve.session(courseStore.current))
const sessionId = computed(() => sess.value.sessionId)
const turns = computed(() => sess.value.turns)
const busy = computed(() => sess.value.busy)
const mastery = computed(() => sess.value.mastery)
const masteryState = computed(() => sess.value.masteryState)
const resolved = computed(() => sess.value.resolved)
const citations = computed(() => sess.value.citations)
const answer = computed({
  get: () => sess.value.answer,
  set: (v: string) => solve.setAnswer(courseStore.current, v),
})

// 还没开始解题时的题目输入框（开始之后题目由 store 持有）
const problem = ref('')
const scroller = ref<HTMLElement | null>(null)

async function scrollDown() {
  await nextTick()
  scroller.value?.scrollTo({ top: scroller.value.scrollHeight, behavior: 'smooth' })
}
watch(() => turns.value.length, scrollDown)
watch(() => turns.value[turns.value.length - 1]?.text?.length, scrollDown)

// 从拍照批改带过来的题目和转写稿（docs/35 P0-5）。题目进起始框；转写稿是学生
// 自己的解法，等会话开起来再放进作答框 —— 它是"我的作答"，不是题目。
const route = useRoute()
const router = useRouter()
const pendingAnswer = ref('')
function takePrefill() {
  const q = route.query
  const p = typeof q.problem === 'string' ? q.problem : ''
  const a = typeof q.answer === 'string' ? q.answer : ''
  if (!p && !a) return
  if (sessionId.value) solve.reset(courseStore.current)   // 旧会话已落库，换一题不丢记录
  problem.value = p
  pendingAnswer.value = a
  router.replace({ name: 'solve' })                        // 刷新时不重复灌入
}

// 起始页的"从你的薄弱点挑一道"：原先只有一个空输入框、下面整片空白，演示时还得现场找题粘贴。
// 取的是练习页同一份推荐（薄弱点优先），只列题库里的题 —— 题干完整、答案可判。
const picks = ref<{ problem_id: number; knowledge: string; stem: string; reason: string }[]>([])
async function loadPicks() {
  if (sessionId.value) return
  try {
    const r = await get<{ items: any[] }>(
      `/practice/recommend?course=${encodeURIComponent(courseStore.current)}&limit=6&per_point=1`)
    picks.value = (r.items || []).filter((i: any) => i.source === 'bank' && i.stem).slice(0, 3)
  } catch { picks.value = [] }
}
function usePick(p: { stem: string }) { problem.value = p.stem }

onActivated(async () => {
  await solve.restore(courseStore.current)
  takePrefill()
  loadPicks()
  scrollDown()
})
// 切科目：各门各存各的会话，读回那门自己的
watch(() => courseStore.current, async (c) => { problem.value = ''; await solve.restore(c); loadPicks() })

async function startSession() {
  await solve.start(courseStore.current, problem.value)
  problem.value = ''
  if (pendingAnswer.value) { answer.value = pendingAnswer.value; pendingAnswer.value = '' }
  scrollDown()
}

// 请助教看看这次诊断（docs/35 P1-2）：标记的是刚写进错题本的那一条
async function requestReview(t: any) {
  const id = t.data?.error_record_id
  if (!id || t.data.reviewRequested) return
  try {
    await post('/review-requests', { error_record_id: id })
    t.data.reviewRequested = true
    solve.persist(true)
    toast('已提交给助教，结论会显示在错题本这条记录下面')
  } catch { /* 拦截器已提示 */ }
}
function submit() { solve.submit(courseStore.current) }
function moreHint() { solve.hint(courseStore.current) }
function makeVariant() { solve.variant(courseStore.current) }
function reset() { solve.reset(courseStore.current) }

const masteryPct = () => Math.round((mastery.value ?? 0) * 100)

/* ---------- 闭环步骤指示（docs/35 §2.2） ----------
 * 朋友提议把"十环主线"铺在首页，那会和"首页只一个主任务"冲突；主线放在这里 ——
 * 学生正在走的就是这条线，亮到哪一步一眼可见。每一步都从真实对话流推出来，不预设。 */
const steps = computed(() => {
  const ts = turns.value
  const has = (k: string) => ts.some((t) => t.kind === k)
  const vIdx = ts.findIndex((t) => t.kind === 'variant')
  const afterVariant = vIdx >= 0 ? ts.slice(vIdx + 1) : []
  const diag = ts.find((t) => t.kind === 'diagnosis')
  return [
    { label: '写思路', done: ts.some((t) => t.role === 'student') },
    { label: '分级提示', done: has('hint') },
    { label: '定位首错', done: !!diag },
    { label: '换法重讲', done: !!diag?.data?.reexplain },
    { label: '变式题', done: vIdx >= 0 },
    { label: '复测', done: afterVariant.some((t) => t.kind === 'correct') },
    { label: '已记录', done: has('diagnosis') || has('correct') },
  ]
})
const stateLabel: Record<string, string> = {
  not_started: '未开始', learning: '学习中', fragile: '不稳固',
  mastered: '已掌握', needs_review: '待复习', misconception: '概念错误',
}
</script>

<template>
  <div class="wrap">
    <CLPageHeader
      title="解题纠错"
      sub="先写思路，AI 助手分级提示、找出第一个错步，不直接给答案"
    >
      <template #actions>
        <CLBadge v-if="mastery !== null" :variant="masteryState === 'misconception' ? 'err' : 'ok'">
          掌握度 {{ masteryPct() }}% · {{ stateLabel[masteryState] || '—' }}
        </CLBadge>
      </template>
    </CLPageHeader>

    <!-- 起始：输入题目 -->
    <div v-if="!sessionId" class="cl-card starter">
      <CLField v-model="problem" label="题目" textarea :placeholder="problemPlaceholder" />
      <CLButton :disabled="busy || !problem.trim()" @click="startSession">{{ busy ? '准备中…' : '开始解题' }}</CLButton>
      <div v-if="picks.length" class="picks">
        <div class="picks-head">没有现成的题？从你的薄弱点挑一道</div>
        <button v-for="p in picks" :key="p.problem_id" type="button" class="pick" @click="usePick(p)">
          <CLBadge variant="cite">{{ p.knowledge }}</CLBadge>
          <span class="pick-stem">{{ p.stem }}</span>
          <span class="pick-why">{{ p.reason }}</span>
        </button>
      </div>
    </div>

    <!-- 会话进行中 -->
    <template v-else>
      <div class="cl-card problem-bar">
        <div class="pb-label">{{ sess.originProblem ? '当前：变式题' : '当前题目' }}</div>
        <div class="pb-text"><MarkdownView :source="sess.problem" /></div>
        <details v-if="sess.originProblem" class="pb-origin">
          <summary>原题</summary>
          <MarkdownView :source="sess.originProblem" />
        </details>
        <CLCitations v-if="citations.length" class="pb-cites" :citations="citations" />
        <CLButton variant="text" @click="reset">换一题</CLButton>
      </div>

      <ol class="loop-steps" aria-label="学习闭环进度">
        <li v-for="(st, k) in steps" :key="k" :class="{ done: st.done }">{{ st.label }}</li>
      </ol>

      <div ref="scroller" class="stream">
        <div v-for="(t, i) in turns" :key="i" :class="['row', t.role]">
          <!-- 学生 -->
          <div v-if="t.role === 'student'" class="bubble student">{{ t.text }}</div>

          <!-- 助教·实时思考 -->
          <CLThinkBlock v-else-if="t.kind === 'think'" class="think-turn" :streaming="t.streaming" :reasoning="t.text" label="推理" />

          <!-- 助教·追问/正确/提示文本 -->
          <div v-else class="bubble tutor">
            <template v-if="t.kind === 'error'">
              <CLBadge variant="warn">这一步没完成</CLBadge>
              <div class="tutor-text">{{ t.text }}</div>
            </template>
            <template v-else-if="t.kind === 'ask' || t.kind === 'correct'">
              <CLBadge v-if="t.kind === 'correct'" variant="ok">作答正确</CLBadge>
              <div class="tutor-text"><MarkdownView :source="t.text || ''" /></div>
            </template>

            <!-- 诊断卡 -->
            <template v-else-if="t.kind === 'diagnosis'">
              <div class="diag-head">
                <CLBadge variant="err">第 {{ t.data.first_wrong_step }} 步开始有误</CLBadge>
                <CLBadge v-for="(n, k) in t.data.error_tag_names" :key="k" variant="warn">{{ n }}</CLBadge>
              </div>
              <div class="tutor-text"><b>诊断：</b><MarkdownView :source="t.data.diagnosis || ''" /></div>
              <div v-if="t.data.reexplain" class="tutor-text reexplain"><b>换个讲法：</b><MarkdownView :source="t.data.reexplain" /></div>
              <div v-if="t.data.remediation" class="tutor-text"><b>下一步：</b><MarkdownView :source="t.data.remediation" /></div>
              <!-- 诊断也带教材依据：以前只有解题那一栏有引用，诊断说"你错了"却说不出出处 -->
              <CLCitations v-if="t.data.citations?.length" class="diag-cites" :citations="t.data.citations" />
              <p class="hint-tip">{{ t.data.next }}</p>
              <div v-if="t.data.error_record_id" class="ask-ta">
                <span v-if="t.data.reviewRequested" class="hint-tip">已请助教复核，结论会显示在错题本里。</span>
                <CLButton v-else variant="text" @click="requestReview(t)">觉得判得不对？请助教看看</CLButton>
              </div>
            </template>

            <!-- 分级提示 -->
            <template v-else-if="t.kind === 'hint'">
              <div class="diag-head"><CLBadge variant="cite">提示 · {{ t.data.title }}</CLBadge></div>
              <div class="tutor-text hint-body"><MarkdownView :source="t.data.content" /></div>
            </template>

            <!-- 变式题 -->
            <template v-else-if="t.kind === 'variant'">
              <div class="diag-head">
                <CLBadge variant="cite">变式题</CLBadge>
                <CLBadge :variant="t.data.verified ? 'ok' : 'warn'">{{ t.data.verified ? '答案已验证' : '待验证' }}</CLBadge>
              </div>
              <div class="tutor-text"><MarkdownView :source="t.data.stem || ''" /></div>
              <p v-if="t.data.hint" class="hint-tip">提示：{{ t.data.hint }}</p>
              <p class="hint-tip">直接在下面作答，这一次按变式题判分，结果会记进学习记录。</p>
            </template>
          </div>
        </div>
      </div>

      <!-- 作答区 -->
      <div class="composer">
        <textarea
          class="cl-textarea"
          v-model="answer"
          placeholder="写下你的思路或解法，Enter 提交 / Shift+Enter 换行"
          @keydown.enter.exact.prevent="submit"
        />
        <div class="composer-actions">
          <CLButton variant="ghost" :disabled="busy" @click="moreHint">要更多提示</CLButton>
          <CLButton v-if="resolved" variant="ghost" :disabled="busy" @click="makeVariant">生成变式题</CLButton>
          <CLButton :disabled="busy || !answer.trim()" @click="submit">{{ busy ? '思考中…' : '提交作答' }}</CLButton>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.wrap { max-width: 920px; margin: 0 auto; display: flex; flex-direction: column; height: 100%; }
.starter { display: flex; flex-direction: column; gap: 14px; }
.picks { display: flex; flex-direction: column; gap: 8px; margin-top: 6px; padding-top: 14px; border-top: 1px solid var(--cl-row-border); }
.picks-head { font-size: 12.5px; font-weight: 700; color: var(--cl-text-3); }
.pick {
  display: grid; grid-template-columns: auto 1fr; gap: 4px 10px; align-items: center; text-align: left;
  padding: 10px 12px; border-radius: var(--cl-radius-sm); cursor: pointer; font: inherit; color: inherit;
  border: 1px solid var(--cl-row-border); background: var(--cl-row);
  transition: background .15s ease, border-color .15s ease;
}
.pick:hover { background: var(--cl-row-hover); border-color: var(--cl-row-border-hover); }
.pick-stem { font-size: 13.5px; color: var(--cl-text); overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.pick-why { grid-column: 2; font-size: 12px; color: var(--cl-text-3); }

/* 题目条：整个会话期间都钉在这里，是"当前在解哪道题"的锚点。
   左侧一条青色竖线把它和下面的对话区分开 —— 它不是一条消息。 */
.problem-bar {
  display: flex; flex-wrap: wrap; align-items: center; gap: 10px;
  padding: 14px 18px; margin-bottom: 14px;
  border-left: 3px solid var(--cl-accent);
  box-shadow: var(--cl-glass-shadow), var(--cl-edge-glow-accent);
}
.pb-label { font-size: 12px; font-weight: 700; color: var(--cl-accent-soft); letter-spacing: .06em; }
.pb-text { flex: 1; min-width: 200px; font-weight: 600; }
.pb-origin { flex-basis: 100%; font-size: 12.5px; color: var(--cl-text-3); }
.pb-origin summary { cursor: pointer; }
.ask-ta { display: flex; justify-content: flex-end; }
.loop-steps { display: flex; flex-wrap: wrap; gap: 6px; list-style: none; margin: -4px 0 12px; padding: 0; }
.loop-steps li {
  font-size: 11.5px; padding: 3px 10px; border-radius: var(--cl-radius-pill);
  border: 1px solid var(--cl-row-border); color: var(--cl-text-3); background: var(--cl-row);
}
.loop-steps li.done { color: var(--cl-accent-soft); border-color: var(--cl-tint-border); background: var(--cl-tint); }
.diag-cites { margin-top: 10px; }
.pb-cites { flex-basis: 100%; }

/* ---------- 对话气泡 ---------- */
/* 与 Hall 保持同一套材质约定：学生＝实心品牌渐变，助教＝半透明玻璃。
   两页是同一个"和助教对话"的心智模型，样式漂开会让人以为是两个功能。 */
.stream { flex: 1; overflow: auto; display: flex; flex-direction: column; gap: 14px; padding: 4px 2px 12px; }
.row { display: flex; }
.row.student { justify-content: flex-end; }
.think-turn { width: 100%; }
.bubble {
  position: relative; max-width: 82%; padding: 13px 17px;
  border-radius: var(--cl-radius); box-shadow: var(--cl-shadow-md);
}
.bubble.student {
  background: var(--cl-brand-grad); color: var(--cl-on-brand);
  border-bottom-right-radius: 6px; white-space: pre-wrap;
  box-shadow: var(--cl-glow);
}
.bubble.tutor {
  background: var(--cl-glass); border: 1px solid var(--cl-glass-border);
  border-bottom-left-radius: 6px;
  box-shadow: var(--cl-glass-shadow);
}
.bubble.tutor::before {
  content: '';
  position: absolute; left: 17px; right: 17px; top: 0; height: 1px;
  background: var(--cl-glass-hi); pointer-events: none;
}
.tutor-text { margin: 8px 0 0; line-height: 1.7; }
/* 「换个讲法」是诊断卡里嵌的一段重述，压暗并描边，明确它是引用性的插入块 */
.reexplain {
  background: var(--cl-inset);
  border: 1px solid var(--cl-glass-border);
  border-radius: var(--cl-radius-sm); padding: 10px 12px;
}
.diag-head { display: flex; flex-wrap: wrap; gap: 6px; }
.hint-tip { margin: 10px 0 0; font-size: 13px; color: var(--cl-text-3); }
.hint-body :deep(p) { margin: 4px 0; }

/* 与 Hall 的输入区同一形态。.stream 自己裁内容，不需要 backdrop-filter。 */
.composer {
  position: relative;
  display: flex; gap: 10px; align-items: flex-end;
  margin-top: 4px; padding: 13px 2px 0;
  border-top: 1px solid var(--cl-glass-border);
}
.composer::before {
  content: '';
  position: absolute; left: 0; right: 0; top: -1px; height: 1px;
  background: var(--cl-glass-hi); pointer-events: none;
}
.composer .cl-textarea { min-height: 58px; }
.composer-actions { display: flex; gap: 8px; flex-shrink: 0; flex-wrap: wrap; }
</style>
