<script setup lang="ts">
/**
 * 入学摸底 / 自适应分级测试。
 *
 * 这页面对的是"新账号第一次进来看到什么"——在它之前，学习中心、练习页、复习计划
 * 三处各自显示一句「还没有数据」就没有下文了。摸底十来道题把水平量出来，
 * 给测到的知识点写上掌握度初值，下游那一整条链路立刻有东西可算。
 *
 * 三条渲染纪律：
 *
 * 1) **测的时候不给答案，测完一次性回顾**。中途告诉他对错就够了（不然一路蒙下来
 *    不知道自己在干嘛），但把正确答案摊开就变成了边学边测，后面几题的档位不作数。
 * 2) **写进去的和没写进去的要分清**。摸底只给"之前没有掌握度记录"的知识点写初值，
 *    已有记录的点照样报档位但标明未覆盖 —— 不说清楚的话，人会以为整门课都被重估了。
 * 3) **覆盖率如实报**。116 个知识点摸底顶多测到十来个，结果页把"测到 N / 共 M"
 *    印在显眼处；藏起来就等于暗示这份分级是全门的结论。
 */
import { computed, onActivated, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { get, post } from '@/api/client'
import { useCourseStore } from '@/stores/course'
import CLBadge from '@/components/ui/CLBadge.vue'
import CLButton from '@/components/ui/CLButton.vue'
import CLCard from '@/components/ui/CLCard.vue'
import CLEmpty from '@/components/ui/CLEmpty.vue'
import CLPageHeader from '@/components/ui/CLPageHeader.vue'
import CLProgress from '@/components/ui/CLProgress.vue'
import CLSegmented from '@/components/ui/CLSegmented.vue'
import CLSkeleton from '@/components/ui/CLSkeleton.vue'
import CLStat from '@/components/ui/CLStat.vue'
import MarkdownView from '@/components/MarkdownView.vue'
import { splitChoiceOptions } from '@/utils/stem'

interface Item {
  seq: number; total: number; problem_id: number; stem: string; type: string
  difficulty: number; knowledge: string; chapter: string
  answer_kind: string; answer_unit: string; answer_multi: boolean
}
interface ReviewRow {
  seq: number; stem: string; difficulty: number; knowledge: string; chapter: string
  your_answer: string; answer: string; correct: boolean | null; details: string
}
interface Result {
  session_id: number; course_display: string; finished: boolean
  total: number; answered: number; judged: number; correct: number; undetermined: number
  duration_seconds: number
  level: number | null; level_name: string; level_hint: string
  level_mastery: number | null; next_difficulty: number
  coverage: {
    points_tested: number; points_total: number; points_written: number
    chapters_tested: number; chapters_total: number
  }
  points: {
    knowledge_id: number; name: string; chapter: string; level: number
    mastery: number; asked: number; correct_n: number; skipped: boolean
  }[]
  chapters: { chapter: string; asked: number; correct_n: number; level: number; mastery: number }[]
  review: ReviewRow[]
}
interface Status {
  ready: boolean; pool_size: number; min_items: number
  chapters_available: number; chapters_total: number
  active: { session_id: number; answered: number; total: number; item: Item | null } | null
  last: Result | null
}

const router = useRouter()
const courseStore = useCourseStore()

const loading = ref(true)
const status = ref<Status | null>(null)
const sessionId = ref(0)
const item = ref<Item | null>(null)
const result = ref<Result | null>(null)
const busy = ref(false)
const length = ref('12')
const lengthOptions = [{ label: '8 题', value: '8' }, { label: '12 题', value: '12' }, { label: '16 题', value: '16' }]
// 上一题的判定。摸底不给答案，但总得让人知道刚才那下算对了没有。
const lastVerdict = ref<boolean | null | 'none'>('none')
const openedAt = ref(Date.now())

const running = computed(() => !!sessionId.value && !!item.value)
const progress = computed(() => {
  if (!item.value) return 0
  return ((item.value.seq - 1) / Math.max(1, item.value.total)) * 100
})

/* ---------- 答题控件：与练习页同一套（按 answer_kind 渲染） ---------- */
const CHOICES = ['A', 'B', 'C', 'D']
const answerText = ref('')
const choiceSel = ref<string[]>([])
const numVal = ref('')
const control = computed(() => {
  const k = item.value?.answer_kind
  if (k === 'choice') return 'choice'
  if (k === 'scalar' || k === 'percent' || k === 'int') return 'number'
  return 'token'
})
const numberPlaceholder = computed(() => {
  const k = item.value?.answer_kind
  if (k === 'int') return '整数，如 12'
  if (k === 'percent') return '如 37.5'
  return '数值，如 1.5'
})
const tokenPlaceholder = computed(() => {
  const k = item.value?.answer_kind
  if (k === 'bits') return '二进制串，如 11110011'
  if (k === 'ipv4net') return '网络/前缀，如 192.168.1.0/26'
  if (k === 'set') return '多个答案用逗号分隔，如 P1,P3,P4'
  return '规范答案'
})
watch(numVal, (v) => {
  answerText.value = v === '' ? '' : (item.value?.answer_kind === 'percent' ? `${v}%` : String(v))
})
function toggleChoice(c: string) {
  if (item.value?.answer_multi) {
    const i = choiceSel.value.indexOf(c)
    if (i >= 0) choiceSel.value.splice(i, 1)
    else choiceSel.value.push(c)
    answerText.value = [...choiceSel.value].sort().join(',')
  } else {
    choiceSel.value = [c]
    answerText.value = c
  }
}
function resetInput() {
  answerText.value = ''; choiceSel.value = []; numVal.value = ''
  openedAt.value = Date.now()
}

/* ---------- 流程 ---------- */
async function load() {
  loading.value = true
  try {
    const s = await get<Status>('/placement/status?course=' + encodeURIComponent(courseStore.current))
    status.value = s
    result.value = s.active ? null : s.last
    sessionId.value = s.active?.session_id || 0
    item.value = s.active?.item || null
    lastVerdict.value = 'none'
    resetInput()
  } finally {
    loading.value = false
  }
}

async function begin(restart = false) {
  if (busy.value) return
  busy.value = true
  try {
    const r = await post<{ session_id: number; item: Item; resumed: boolean }>(
      '/placement/start',
      { course: courseStore.current, length: Number(length.value), restart })
    sessionId.value = r.session_id
    item.value = r.item
    result.value = null
    lastVerdict.value = 'none'
    resetInput()
  } catch {
    /* 失败原因由全局拦截器提示（题库不足会返回 503 + 说明） */
  } finally {
    busy.value = false
  }
}

async function submit() {
  if (busy.value || !answerText.value.trim() || !sessionId.value) return
  busy.value = true
  try {
    const r = await post<{ correct: boolean | null; finished: boolean; item?: Item; result?: Result }>(
      '/placement/answer',
      {
        session_id: sessionId.value,
        answer: answerText.value,
        duration: Math.max(1, Math.round((Date.now() - openedAt.value) / 1000)),
      })
    lastVerdict.value = r.correct
    if (r.finished && r.result) {
      result.value = r.result
      item.value = null
      sessionId.value = 0
      await load()
    } else if (r.item) {
      item.value = r.item
      resetInput()
    }
  } finally {
    busy.value = false
  }
}

async function quit() {
  if (!sessionId.value) return
  await post('/placement/abandon', { session_id: sessionId.value })
  await load()
}

const verdictText = computed(() => {
  if (lastVerdict.value === 'none') return ''
  if (lastVerdict.value === null) return '上一题没能判定你的作答，这题不计入分级'
  return lastVerdict.value ? '上一题答对了，难度往上走一档' : '上一题答错了，难度往下走一档'
})

function fmtDuration(seconds: number): string {
  if (!seconds) return '—'
  const m = Math.floor(seconds / 60)
  return m ? `${m} 分 ${seconds % 60} 秒` : `${seconds} 秒`
}

onActivated(async () => { await courseStore.load(); await load() })
watch(() => courseStore.current, load)
</script>

<template>
  <div class="placement">
    <CLPageHeader title="入学摸底"
      sub="十来道自适应客观题，测完给各知识点定一个起点">
      <template #actions>
        <CLBadge variant="cite">{{ courseStore.info?.short }}</CLBadge>
      </template>
    </CLPageHeader>

    <CLSkeleton v-if="loading" :rows="6" />

    <!-- ============ 题库撑不起一次摸底 ============ -->
    <CLCard v-else-if="status && !status.ready && !running">
      <CLEmpty title="这门课还不够摸底"
        :hint="`摸底只用可当场判定的客观题（选择 / 数值 / 位串），当前题库里只有 ${status.pool_size} 道，至少需要 ${status.min_items} 道。`">
        <p class="thin-note">
          这门课的客观题还在补充，可以先换一门课做摸底。
        </p>
      </CLEmpty>
    </CLCard>

    <!-- ============ 答题中 ============ -->
    <template v-else-if="running && item">
      <CLCard class="quiz">
        <div class="quiz-top">
          <div class="quiz-meta">
            <CLBadge variant="cite">第 {{ item.seq }} / {{ item.total }} 题</CLBadge>
            <CLBadge>难度 {{ item.difficulty }}</CLBadge>
            <CLBadge v-if="item.chapter">{{ item.chapter }}</CLBadge>
          </div>
          <CLButton variant="text" @click="quit">放弃这次摸底</CLButton>
        </div>
        <CLProgress :value="progress" :right="`${item.seq - 1}/${item.total}`" />

        <!-- 判定条：只说对错，不给答案。给了答案，后面几题就不是在测水平了。 -->
        <p v-if="verdictText" class="verdict"
          :class="{ ok: lastVerdict === true, bad: lastVerdict === false, unk: lastVerdict === null }">
          {{ verdictText }}
        </p>

        <div class="stem"><MarkdownView :source="splitChoiceOptions(item.stem)" /></div>

        <div v-if="control === 'choice'" class="choices">
          <button v-for="c in CHOICES" :key="c" type="button"
            :class="['choice', { on: choiceSel.includes(c) }]" @click="toggleChoice(c)">{{ c }}</button>
          <span class="choice-hint">{{ item.answer_multi ? '可多选' : '单选' }}</span>
        </div>
        <div v-else-if="control === 'number'" class="num-input">
          <input class="cl-input" type="text" inputmode="decimal" v-model="numVal" :placeholder="numberPlaceholder" />
          <span v-if="item.answer_kind === 'percent'" class="suffix">%</span>
          <span v-else-if="item.answer_unit" class="suffix">{{ item.answer_unit }}</span>
        </div>
        <input v-else class="cl-input mono" type="text" v-model="answerText" :placeholder="tokenPlaceholder" />

        <div class="quiz-actions">
          <CLButton :disabled="busy || !answerText.trim()" @click="submit">
            {{ busy ? '判定中…' : (item.seq >= item.total ? '提交并出报告' : '提交，下一题') }}
          </CLButton>
          <span class="thin-note">摸底过程中不给提示、不显示答案，做完后逐题回顾。</span>
        </div>
      </CLCard>
    </template>

    <!-- ============ 结果 / 开始 ============ -->
    <template v-else>
      <CLCard v-if="!result" class="intro">
        <h2>先花几分钟，把底摸清楚</h2>
        <p>
          推荐、复习计划和薄弱点排序都根据你的作答来定，新账号还没有依据。
          摸底会从
          <b>{{ status?.chapters_available || 0 }}</b> 个有题的章节里自适应出题：答对就往上加难度，
          答错就往下降，测完把测到的知识点写上掌握度初值。
        </p>
        <ul class="intro-list">
          <li>全程客观题，当场判定，同样的作答给出同样的档位。</li>
          <li>只给<b>测到的</b>知识点写初值；已经有学习记录的点不会被摸底冲掉。</li>
          <li>中途关掉也没关系，进度记在服务端，回来接着答。</li>
        </ul>
        <div class="intro-actions">
          <div class="ctl"><span>题量</span><CLSegmented v-model="length" :options="lengthOptions" /></div>
          <CLButton :disabled="busy" @click="begin(false)">{{ busy ? '准备中…' : '开始摸底' }}</CLButton>
        </div>
      </CLCard>

      <template v-if="result">
        <!-- 定级卡：整页的结论 -->
        <CLCard class="level-card">
          <span class="level-glow" aria-hidden="true"></span>
          <div class="level-copy">
            <div class="eyebrow"><span class="eyebrow-dot" aria-hidden="true"></span>摸底结论</div>
            <h2>{{ result.level_name }} · 第 {{ result.level }} 档</h2>
            <p>{{ result.level_hint }}</p>
            <div class="dots">
              <i v-for="n in 5" :key="n" :class="{ on: (result.level || 0) >= n }" />
              <small>后续练习会从难度 {{ result.next_difficulty }} 起推题</small>
            </div>
          </div>
          <div class="level-actions">
            <CLButton @click="router.push({ name: 'practice' })">按这个档位开始练</CLButton>
            <CLButton variant="ghost" @click="begin(true)">重新摸底</CLButton>
          </div>
        </CLCard>

        <div class="stats">
          <CLStat :value="`${result.correct}/${result.judged}`" label="判定题数答对" accent />
          <CLStat :value="fmtDuration(result.duration_seconds)" label="用时" />
          <CLStat :value="`${result.coverage.points_tested}/${result.coverage.points_total}`" label="覆盖知识点" />
          <CLStat :value="`${result.coverage.chapters_tested}/${result.coverage.chapters_total}`" label="覆盖章节" />
        </div>

        <p v-if="result.undetermined" class="thin-note wide">
          有 {{ result.undetermined }} 题没能判定你的作答（格式没对上），它们既没算对也没算错，不参与定级。
        </p>

        <div class="grid">
          <CLCard title="各章水平" sub="只统计这次测到的章节">
            <ul class="ch-list">
              <li v-for="ch in result.chapters" :key="ch.chapter">
                <CLProgress :value="ch.mastery * 100" :label="ch.chapter"
                  :right="`第 ${ch.level} 档 · ${ch.correct_n}/${ch.asked}`"
                  :tone="ch.level <= 1 ? 'err' : (ch.level <= 3 ? 'warn' : 'brand')" />
              </li>
            </ul>
          </CLCard>

          <CLCard title="写入的掌握度初值" :sub="`${result.coverage.points_written} 个知识点已写入，其余保持未评估`">
            <ul class="pt-list">
              <li v-for="p in result.points" :key="p.knowledge_id" :class="{ skipped: p.skipped }">
                <div class="pt-top">
                  <b>{{ p.name }}</b>
                  <CLBadge v-if="p.skipped" variant="warn">已有记录，未覆盖</CLBadge>
                  <CLBadge v-else variant="ok">{{ Math.round(p.mastery * 100) }}%</CLBadge>
                </div>
                <small>{{ p.chapter || '未归章' }} · 第 {{ p.level }} 档 · {{ p.correct_n }}/{{ p.asked }} 对</small>
              </li>
            </ul>
            <p class="thin-note">
              摸底结果只是起点，每个知识点只测了一两道题，后面正常做题会很快更新它。
            </p>
          </CLCard>
        </div>

        <CLCard title="逐题回顾" sub="现在才给正确答案：测的过程中给了，后面几题就不作数了">
          <ul class="rv-list">
            <li v-for="r in result.review" :key="r.seq"
              :class="{ ok: r.correct === true, bad: r.correct === false, unk: r.correct === null }">
              <div class="rv-head">
                <span class="rv-seq">{{ r.seq }}</span>
                <CLBadge :variant="r.correct === true ? 'ok' : (r.correct === false ? 'err' : 'warn')">
                  {{ r.correct === true ? '答对' : (r.correct === false ? '答错' : '未判定') }}
                </CLBadge>
                <CLBadge variant="cite">{{ r.knowledge }}</CLBadge>
                <CLBadge>难度 {{ r.difficulty }}</CLBadge>
              </div>
              <div class="rv-stem"><MarkdownView :source="splitChoiceOptions(r.stem)" /></div>
              <div class="rv-ans">
                <span>你的作答：<b>{{ r.your_answer || '（空）' }}</b></span>
                <span>参考答案：<b>{{ r.answer }}</b></span>
              </div>
            </li>
          </ul>
          <p class="thin-note">答错的题已经进了错题本，复习计划会把它们排进去。</p>
        </CLCard>
      </template>
    </template>
  </div>
</template>

<style scoped>
.placement { max-width: 1040px; margin: 0 auto; }
.thin-note { margin: 10px 0 0; font-size: 12.5px; line-height: 1.7; color: var(--cl-text-3); }
.thin-note.wide { margin: 0 0 16px; }
.thin-note code { font-family: var(--cl-mono); }

/* ---------- 开始页 ---------- */
.intro h2 { margin: 0 0 8px; font-size: 20px; letter-spacing: .2px; }
.intro p { margin: 0; color: var(--cl-text-2); line-height: 1.8; }
.intro-list { margin: 14px 0 0; padding-left: 18px; color: var(--cl-text-3); font-size: 13.5px; line-height: 1.9; }
.intro-actions {
  display: flex; align-items: flex-end; gap: 18px; flex-wrap: wrap;
  margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--cl-row-border);
}
.ctl { display: flex; flex-direction: column; gap: 7px; font-size: 12.5px; font-weight: 600; color: var(--cl-text-2); }

/* ---------- 答题 ---------- */
.quiz { display: flex; flex-direction: column; gap: 14px; }
.quiz-top { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.quiz-meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
/* 判定条压得很轻：它是一句过场提示，不该盖过题干。
   三态各自有色，"未判定"用琥珀而不是红 —— 那是格式没对上，不是答错。 */
.verdict {
  margin: 0; padding: 8px 12px; font-size: 13px;
  border-radius: var(--cl-radius-sm); border-left: 3px solid var(--cl-border-strong);
  background: var(--cl-inset); color: var(--cl-text-2);
}
.verdict.ok { border-left-color: var(--cl-ok); color: var(--cl-ok-fg); }
.verdict.bad { border-left-color: var(--cl-warn); color: var(--cl-warn-fg); }
.verdict.unk { border-left-color: var(--cl-warn); }
.stem { line-height: 1.75; }
.quiz-actions { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.quiz-actions .thin-note { margin: 0; }

/* 选项矩阵与数值框沿用练习页的写法：同一件事在两页要长得一样 */
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

/* ---------- 定级卡 ---------- */
/* 与学习中心的主推卡同一语言：整页唯一的结论，靠局部光而不是大字号取得重量。 */
.level-card.clcard {
  position: relative; overflow: hidden;
  display: flex; align-items: center; justify-content: space-between; gap: 24px; margin-bottom: 16px;
  background: var(--cl-accent-surface);
  border-color: var(--cl-tint-border);
}
.level-glow {
  position: absolute; left: -60px; top: 50%;
  width: 320px; height: 320px; margin-top: -160px; border-radius: 50%;
  background: var(--cl-accent-halo);
  filter: blur(26px); pointer-events: none;
}
.level-copy { position: relative; min-width: 0; }
.eyebrow { display: flex; align-items: center; gap: 7px; color: var(--cl-accent-soft); font-size: 12px; font-weight: 800; letter-spacing: .08em; }
.eyebrow-dot { width: 6px; height: 6px; border-radius: 50%; flex: none; background: var(--cl-accent); box-shadow: var(--cl-dot-glow); }
.level-copy h2 { margin: 7px 0 5px; font-size: 22px; letter-spacing: .2px; }
.level-copy p { margin: 0; color: var(--cl-text-2); }
.dots { display: flex; align-items: center; gap: 6px; margin-top: 12px; }
.dots i {
  width: 26px; height: 6px; border-radius: 3px; background: var(--cl-inset-deep);
}
.dots i.on { background: linear-gradient(90deg, var(--cl-brand), var(--cl-accent)); box-shadow: var(--cl-dot-glow-brand); }
.dots small { margin-left: 8px; font-size: 12px; color: var(--cl-text-3); }
.level-actions { position: relative; display: flex; flex-direction: column; gap: 8px; flex: none; }

.stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 16px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; align-items: start; margin-bottom: 18px; }

.ch-list, .pt-list, .rv-list { list-style: none; margin: 0; padding: 0; }
.ch-list { display: flex; flex-direction: column; gap: 12px; }
.pt-list { display: flex; flex-direction: column; gap: 8px; }
.pt-list li {
  padding: 9px 11px; border-radius: var(--cl-radius-sm);
  background: var(--cl-row); border: 1px solid var(--cl-row-border);
}
/* 未覆盖的点压暗一档：它出现在"写入的初值"这张卡里，必须一眼看出没写。 */
.pt-list li.skipped { opacity: .62; }
.pt-top { display: flex; align-items: center; gap: 8px; }
.pt-top b { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pt-list small { display: block; margin-top: 5px; font-size: 12px; color: var(--cl-text-3); }

/* ---------- 逐题回顾 ---------- */
.rv-list { display: flex; flex-direction: column; gap: 10px; }
.rv-list li {
  padding: 11px 13px; border-radius: var(--cl-radius-sm);
  background: var(--cl-row); border: 1px solid var(--cl-row-border);
  border-left: 3px solid var(--cl-border-strong);
}
.rv-list li.ok { border-left-color: var(--cl-ok); }
.rv-list li.bad { border-left-color: var(--cl-warn); }
.rv-list li.unk { border-left-color: var(--cl-text-3); }
.rv-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.rv-seq { font-family: var(--cl-mono); font-size: 12px; color: var(--cl-text-3); }
.rv-stem { font-size: 13.5px; line-height: 1.6; color: var(--cl-text-2); }
.rv-ans {
  display: flex; gap: 18px; flex-wrap: wrap; margin-top: 9px; padding-top: 9px;
  border-top: 1px dashed var(--cl-row-border); font-size: 13px; color: var(--cl-text-3);
}
.rv-ans b { color: var(--cl-text); font-family: var(--cl-mono); }

@media (max-width: 820px) { .stats, .grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 620px) {
  .stats, .grid { grid-template-columns: 1fr; }
  .level-card.clcard { flex-direction: column; align-items: flex-start; }
  .num-input .cl-input { width: 100%; }
}
@media (prefers-reduced-motion: reduce) { .choice { transition: none; } }
</style>
