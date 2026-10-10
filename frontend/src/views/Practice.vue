<script setup lang="ts">
import { ref, computed, watch, onActivated, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { get, post } from '@/api/client'
import { postSSE, sseErrorText } from '@/api/sse'
import { useCourseStore } from '@/stores/course'
import CLPageHeader from '@/components/ui/CLPageHeader.vue'
import CLButton from '@/components/ui/CLButton.vue'
import CLBadge from '@/components/ui/CLBadge.vue'
import CLCitations from '@/components/ui/CLCitations.vue'
import CLSegmented from '@/components/ui/CLSegmented.vue'
import CLThinkBlock from '@/components/ui/CLThinkBlock.vue'
import CLProgress from '@/components/ui/CLProgress.vue'
import CLSkeleton from '@/components/ui/CLSkeleton.vue'
import CLEmpty from '@/components/ui/CLEmpty.vue'
import MarkdownView from '@/components/MarkdownView.vue'
import { splitChoiceOptions } from '@/utils/stem'

const courseStore = useCourseStore()
const route = useRoute()
const router = useRouter()
const taskId = ref(0)
const taskKnowledgeId = ref(0)
const openedAt = ref(Date.now())
const DS_FALLBACK = ['栈的应用', '队列', '链表', '二叉树遍历', '树', '二分查找', '哈希表', '快速排序', '排序', '递归与分治', '动态规划', '复杂度分析']
// 知识点来自课程注册表；未就绪时数据结构用兜底
const KNOWLEDGE = computed(() => {
  const t = courseStore.info?.topics
  return t && t.length ? t : (courseStore.current === 'ds-algo' ? DS_FALLBACK : [])
})
const mode = ref('recommend')
const modeOptions = [{ label: '为我推荐', value: 'recommend' }, { label: '固定题库', value: 'bank' }, { label: 'AI 生成', value: 'generate' }]
const recStateLabel: Record<string, string> = {
  not_started: '未开始', learning: '学习中', fragile: '不稳固', mastered: '已掌握',
  needs_review: '待复习', misconception: '概念错误',
}

/* ---------- 个性化推荐 ---------- */
const recommendations = ref<any>({ weak_points: [], items: [], cold_start: true })
const recLoading = ref(false)
const recKnowledgeId = ref(0)
const recItems = computed(() => (recommendations.value.items || [])
  .filter((x: any) => !recKnowledgeId.value || x.knowledge_id === recKnowledgeId.value))

async function loadRecommend(knowledgeId = 0) {
  recLoading.value = true
  try {
    const q = new URLSearchParams({ course: courseStore.current, limit: '6', per_point: '3' })
    if (knowledgeId) q.set('knowledge_id', String(knowledgeId))
    recommendations.value = await get('/practice/recommend?' + q.toString())
    const points = recommendations.value.weak_points || []
    if (!points.some((p: any) => p.knowledge_id === recKnowledgeId.value)) {
      recKnowledgeId.value = points[0]?.knowledge_id || 0
    }
  } finally {
    recLoading.value = false
  }
}

async function openRecommended(item: any) {
  if (item.source !== 'bank') return
  bankFilter.value = item.knowledge || ''
  mode.value = 'bank'
  await loadBank()
  await openProblem(item.problem_id)
}

// 生成失败时的替代出口：同一个知识点，直接去做题库里已验证过的题
async function bankForGen() {
  bankFilter.value = gen.value?.knowledge || kp.value || ''
  mode.value = 'bank'
  await loadBank()
}

async function generateRecommended(item: any) {
  kp.value = item.knowledge
  genKnowledgeId.value = item.knowledge_id || 0
  genErrorType.value = item.error_type || ''
  difficulty.value = String(item.difficulty || 0)
  mode.value = 'generate'
  await generate()
}

/* ---------- 固定题库 ---------- */
const bankList = ref<any[]>([])
const bankDist = ref<any[]>([])       // 知识点题量分布（GET /problems 已返回）
const bankFilter = ref('')
const distMax = computed(() => Math.max(1, ...bankDist.value.map((d: any) => d.cnt || 0)))
const bankTotal = computed(() => bankDist.value.reduce((s: number, d: any) => s + (d.cnt || 0), 0))
const cur = ref<any>(null)          // 当前题目详情

// 题目出处（出题时依据的教材片段）。老题没有这一列，读回来是空数组。
const stemSources = computed<string[]>(() => {
  const cites = cur.value?.citations
  if (!Array.isArray(cites)) return []
  return cites.map((c: any) => {
    const page = c.page_start
      ? (c.page_end && c.page_end !== c.page_start ? ` P${c.page_start}–${c.page_end}` : ` P${c.page_start}`)
      : ''
    return `《${c.source}》${c.title_path ? ' ' + c.title_path : ''}${page}`
  })
})
const bankAnswer = ref('')
const bankResult = ref<any>(null)
const bankBusy = ref(false)

async function loadBank() {
  const params = new URLSearchParams({ course: courseStore.current })
  if (bankFilter.value) params.set('knowledge', bankFilter.value)
  const r: any = await get('/problems?' + params.toString())
  bankList.value = r.items || []
  bankDist.value = r.distribution || []
  cur.value = null; bankResult.value = null
}
async function openProblem(id: number) {
  bankResult.value = null; bankAnswer.value = ''
  bankHint.value = ''; hintsUsed.value = 0
  choiceSel.value = []; numVal.value = ''
  cur.value = await get('/problems/' + id)
  openedAt.value = Date.now()
}
// 提示按需拉取并计数。这个次数不是装饰：它会进 mastery.update_mastery 的
// hint_penalty（看了提示才做对，涨得少）和 spaced_review.next_interval 的
// 「依赖提示则不进阶」。原先前端根本不传，那两条分支一直是死代码。
const bankHint = ref('')
const hintsUsed = ref(0)
const hintBusy = ref(false)

async function revealHint() {
  if (hintBusy.value || !cur.value || bankHint.value) return
  hintBusy.value = true
  try {
    const r = await get<{ hint: string }>(`/problems/${cur.value.id}/hint`)
    bankHint.value = r.hint
    hintsUsed.value += 1
  } finally {
    hintBusy.value = false
  }
}

async function judge() {
  if (bankBusy.value || !cur.value || !bankAnswer.value.trim()) return
  bankBusy.value = true
  try {
    const isCode = cur.value.verification_type === 'unit_test'
    bankResult.value = await post('/problems/judge', {
      problem_id: cur.value.id,
      code: isCode ? bankAnswer.value : '',
      answer: isCode ? '' : bankAnswer.value,
      course: courseStore.current,
      task_id: taskId.value,
      duration: Math.max(1, Math.round((Date.now() - openedAt.value) / 1000)),
      hints_used: hintsUsed.value,
    })
  } finally {
    bankBusy.value = false
  }
}

// 判题类型 → 展示标签 + 作答框提示
function judgeLabel(vt: string, tc?: number): string {
  if (vt === 'unit_test') return `代码判题 · ${tc ?? 0} 样例`
  if (vt === 'numeric') return '数值判题'
  if (vt === 'rule') return '概念题 · 得分点校验'
  return '概念题'
}
const answerPlaceholder = computed(() => {
  const vt = cur.value?.verification_type
  if (vt === 'unit_test') return '在此粘贴你的 Python 函数实现…'
  return '在此作答（可分点写出关键结论/步骤）…'
})

// —— 按 answer_kind 渲染结构化答题控件（数值题可靠判分的前提）——
const CHOICES = ['A', 'B', 'C', 'D']
const choiceSel = ref<string[]>([])
const numVal = ref('')
const answerMulti = computed(() => !!cur.value?.answer_multi)
const control = computed(() => {
  const vt = cur.value?.verification_type
  if (vt === 'unit_test') return 'code'
  if (vt === 'numeric') {
    const k = cur.value?.answer_kind
    if (k === 'choice') return 'choice'
    if (k === 'scalar' || k === 'percent' || k === 'int') return 'number'
    return 'token' // bits / ipv4net / set / 未标注
  }
  return 'text' // rule / concept
})
const numberPlaceholder = computed(() => {
  const k = cur.value?.answer_kind
  if (k === 'int') return '整数，如 12'
  if (k === 'percent') return '如 37.5'
  return '数值，如 1.5'
})
const tokenPlaceholder = computed(() => {
  const k = cur.value?.answer_kind
  if (k === 'bits') return '二进制串，如 11110011'
  if (k === 'ipv4net') return '网络/前缀，如 192.168.1.0/26'
  if (k === 'set') return '多个答案用逗号分隔，如 P1,P3,P4'
  return '规范答案'
})
// 数字框 → 拼成干净 token（百分比补 %，其余原样；单位仅展示，判分器会剥离）
watch(numVal, (v) => {
  bankAnswer.value = v === '' ? '' : (cur.value?.answer_kind === 'percent' ? `${v}%` : String(v))
})
function toggleChoice(c: string) {
  if (answerMulti.value) {
    const i = choiceSel.value.indexOf(c)
    if (i >= 0) choiceSel.value.splice(i, 1)
    else choiceSel.value.push(c)
    bankAnswer.value = [...choiceSel.value].sort().join(',')
  } else {
    choiceSel.value = [c]
    bankAnswer.value = c
  }
}

/* ---------- AI 生成 ---------- */
const kp = ref('')
const difficulty = ref('2')
const diffOptions = [{ label: '简单', value: '1' }, { label: '中等', value: '2' }, { label: '较难', value: '4' }]
const gen = ref<any>(null)
const genBusy = ref(false)
const genKnowledgeId = ref(0)
const genErrorType = ref('')

/* ---------- 出题进度（流式） ----------
 * 此前点「生成练习」后只有一个空转的"出题"框，要干等 5–20 秒。出题模型（gpt-4o-mini）
 * 没有可展示的推理过程，这里如实展示系统在做的每一步：检索教材 → 出题（题目边写边出现）
 * → 核对答案 →（不行就说明原因、重出）。 */
interface GenStage { label: string; state: 'active' | 'done' | 'warn' }
const genStages = ref<GenStage[]>([])
const genRaw = ref('')          // 当前这一次出题，模型已经写出来的原文（JSON）
let genAbort: (() => void) | null = null
onUnmounted(() => genAbort?.())

/** 从还没写完的 JSON 里取出 stem 已写出的部分。只认字符串转义，写到一半的转义先不显示。 */
function partialStem(raw: string): string {
  const m = /"stem"\s*:\s*"/.exec(raw)
  if (!m) return ''
  let i = m.index + m[0].length
  let out = ''
  while (i < raw.length) {
    const ch = raw[i]
    if (ch === '"') break
    if (ch !== '\\') { out += ch; i += 1; continue }
    const nx = raw[i + 1]
    if (nx === undefined) break
    if (nx === 'u') {
      const hex = raw.slice(i + 2, i + 6)
      if (hex.length < 4) break
      out += String.fromCharCode(parseInt(hex, 16)); i += 6; continue
    }
    out += nx === 'n' ? '\n' : nx === 't' ? '\t' : nx
    i += 2
  }
  return out
}
const liveStem = computed(() => partialStem(genRaw.value))

function pushStage(label: string, state: GenStage['state'] = 'active') {
  for (const st of genStages.value) if (st.state === 'active') st.state = 'done'
  genStages.value.push({ label, state })
}

function generate() {
  if (genBusy.value || !kp.value) return
  genAbort?.()
  genBusy.value = true
  gen.value = null
  genStages.value = []
  genRaw.value = ''
  genAbort = postSSE('/practice/generate/stream', {
    knowledge_point: kp.value, knowledge_id: genKnowledgeId.value,
    error_type: genErrorType.value, difficulty: Number(difficulty.value), course: courseStore.current,
  }, (ev: any) => {
    if (ev.type === 'stage') {
      if (ev.stage === 'generate') genRaw.value = ''
      pushStage(String(ev.label || ''), ev.stage === 'retry' ? 'warn' : 'active')
    } else if (ev.type === 'token') {
      genRaw.value += String(ev.text || '')
    } else if (ev.type === 'result') {
      for (const st of genStages.value) if (st.state === 'active') st.state = 'done'
      gen.value = ev.problem
    } else if (ev.type === 'error') {
      gen.value = { error: sseErrorText(ev) }
    }
  }, {
    onDone: () => { genBusy.value = false; genAbort = null },
    onError: () => { genBusy.value = false; genAbort = null },
  })
}

// 知识点默认值随课程刷新
watch(KNOWLEDGE, (ks) => { if (!ks.includes(kp.value)) kp.value = ks[0] || '' }, { immediate: true })
// 切换科目：重载题库、清空生成
watch(() => courseStore.current, () => {
  bankFilter.value = ''; gen.value = null; genKnowledgeId.value = 0; genErrorType.value = ''; recKnowledgeId.value = 0
  if (String(route.query.course || '') !== courseStore.current) {
    taskId.value = 0; taskKnowledgeId.value = 0
  }
  loadBank(); loadRecommend()
})

async function applyTaskRoute() {
  const routeCourse = String(route.query.course || '')
  if (routeCourse && routeCourse !== courseStore.current) courseStore.set(routeCourse)
  taskId.value = Number(route.query.task || 0)
  taskKnowledgeId.value = Number(route.query.kid || 0)
  if (taskKnowledgeId.value) {
    mode.value = 'recommend'
    recKnowledgeId.value = taskKnowledgeId.value
    await loadRecommend(taskKnowledgeId.value)
  }
}
watch(() => [route.query.task, route.query.kid, route.query.course], applyTaskRoute)

// 回到页面时刷新题库（会有新生成的题），但**正在作答的题不能被冲掉** ——
// loadBank() 里有 cur.value = null，无条件调用等于把人做了一半的题关掉。
// onActivated 首次挂载也会触发，此时 cur 为空，照常加载。
onActivated(async () => {
  await courseStore.load()
  if (!cur.value) await loadBank()
  await applyTaskRoute()
  if (!taskKnowledgeId.value) await loadRecommend()
})
</script>

<template>
  <div class="wrap">
    <CLPageHeader title="个性化练习" sub="推荐、题库、AI 出题三种练法，答案都会自动核对">
      <template #actions><CLSegmented v-model="mode" :options="modeOptions" /></template>
    </CLPageHeader>

    <div v-if="taskId" class="task-banner">
      <CLBadge variant="cite">本次复习任务</CLBadge>
      <span>完成当前知识点的判题后，会自动回写复习进度。</span>
    </div>

    <!-- ============ 个性化推荐 ============ -->
    <div v-if="mode === 'recommend'" class="recommend">
      <!-- 冷启动提示原先只是一句话，没有出口 —— 「先做几道摸底题」说完就没了下文。
           现在直接指向摸底页，那里能一次把初值测出来。 -->
      <div v-if="recommendations.cold_start" class="cold">
        <CLBadge variant="warn">尚无足够作答记录，下面的排序还只是大纲顺序</CLBadge>
        <CLButton variant="text" @click="router.push({ name: 'placement' })">去做入学摸底</CLButton>
      </div>
      <div class="rec-grid">
        <aside class="cl-card weak-list">
          <div class="rec-title">当前优先补强</div>
          <CLSkeleton v-if="recLoading" :card="false" :rows="5" />
          <template v-else>
            <button v-for="p in recommendations.weak_points" :key="p.knowledge_id"
              :class="['weak', { on: recKnowledgeId === p.knowledge_id }]" @click="recKnowledgeId = p.knowledge_id">
              <div class="weak-head"><b>{{ p.name }}</b><span>{{ Math.round((p.mastery || 0) * 100) }}%</span></div>
              <CLProgress :value="(p.mastery || 0) * 100" :right="recStateLabel[p.state] || p.state" />
              <div class="weak-tags">
                <CLBadge v-if="p.top_error_name" variant="warn">{{ p.top_error_name }}</CLBadge>
                <CLBadge>{{ p.bank_count }} 道题</CLBadge>
              </div>
            </button>
          </template>
        </aside>
        <section class="rec-items">
          <CLSkeleton v-if="recLoading" :rows="5" />
          <CLEmpty v-else-if="!recItems.length" title="暂时没有可推荐内容" hint="可以切到 AI 生成模式按知识点出题" />
          <template v-else>
            <article v-for="(item, i) in recItems" :key="item.problem_id || ('g' + i)" class="cl-card rec-item">
              <div class="d-head">
                <CLBadge variant="cite">{{ item.knowledge }}</CLBadge>
                <CLBadge>难度 {{ item.difficulty }}</CLBadge>
                <CLBadge v-if="item.error_name" variant="warn">针对 {{ item.error_name }}</CLBadge>
              </div>
              <div v-if="item.source === 'bank'" class="rec-stem">{{ splitChoiceOptions(item.stem) }}</div>
              <div v-else class="rec-stem">固定题不足，可按你的掌握度和错因生成一题并先验证答案。</div>
              <p class="rec-reason">{{ item.reason }}</p>
              <!-- 只有排在第一的推荐是主按钮，其余次级：一列同样醒目的「开始作答」等于没有重点 -->
              <CLButton v-if="item.source === 'bank'" :variant="i === 0 ? 'primary' : 'ghost'"
                @click="openRecommended(item)">开始作答</CLButton>
              <CLButton v-else variant="ghost" @click="generateRecommended(item)">生成已验证练习</CLButton>
            </article>
          </template>
        </section>
      </div>
    </div>

    <!-- ============ 固定题库 ============ -->
    <div v-else-if="mode === 'bank'" class="bank">
      <aside class="cl-card list">
        <div class="list-head">
          <select class="cl-input" v-model="bankFilter" @change="loadBank">
            <option value="">全部知识点</option>
            <option v-for="k in KNOWLEDGE" :key="k" :value="k">{{ k }}</option>
          </select>
        </div>
        <div v-if="bankDist.length && !bankFilter" class="dist">
          <div class="dist-head">题量分布 · 共 {{ bankTotal }} 题</div>
          <ul class="dist-list">
            <li v-for="d in bankDist" :key="d.knowledge" class="dist-row"
                @click="bankFilter = d.knowledge; loadBank()">
              <span class="dist-name" :title="d.knowledge">{{ d.knowledge || '未分类' }}</span>
              <span class="dist-bar"><i :style="{ width: (d.cnt / distMax * 100) + '%' }" /></span>
              <span class="dist-cnt">{{ d.cnt }}</span>
            </li>
          </ul>
        </div>
        <ul class="prob-list">
          <li v-for="p in bankList" :key="p.id" :class="['prob', { on: cur && cur.id === p.id }]" @click="openProblem(p.id)">
            <div class="prob-top"><CLBadge variant="cite">{{ p.knowledge }}</CLBadge><span class="prob-diff">难度 {{ p.difficulty }}</span></div>
            <div class="prob-stem">{{ p.stem.replace(/`/g, '') }}</div>
          </li>
        </ul>
        <p v-if="!bankList.length" class="empty">该知识点暂无题目。</p>
      </aside>

      <section v-if="cur" class="cl-card detail">
        <div class="d-head">
          <CLBadge variant="cite">{{ cur.type }}</CLBadge>
          <CLBadge>{{ judgeLabel(cur.verification_type, cur.test_count) }}</CLBadge>
          <span v-if="cur.func_name" class="fn">函数名：<code>{{ cur.func_name }}</code></span>
        </div>
        <div class="d-stem"><MarkdownView :source="splitChoiceOptions(cur.stem)" /></div>
        <!-- 题目出处：出这道题时依据的教材片段。用一行紧凑展示而不是 CLCitations ——
             那个组件是给"回答的教材依据"用的，会展开原文；题目出处只存了定位字段
             （正文在索引里），塞进去会触发它"仅保存了来源信息"那句不适用的提示。 -->
        <p v-if="stemSources.length" class="d-origin">
          <span class="origin-tag">出自</span>
          <span v-for="(o, i) in stemSources" :key="i" class="origin-item">{{ o }}</span>
        </p>

        <!-- 按题型渲染答题控件 -->
        <textarea v-if="control === 'code'" class="cl-textarea code" v-model="bankAnswer" :placeholder="answerPlaceholder" />
        <div v-else-if="control === 'choice'" class="choices">
          <button v-for="c in CHOICES" :key="c" type="button"
            :class="['choice', { on: choiceSel.includes(c) }]" @click="toggleChoice(c)">{{ c }}</button>
          <span class="choice-hint">{{ answerMulti ? '可多选' : '单选' }}</span>
        </div>
        <div v-else-if="control === 'number'" class="num-input">
          <input class="cl-input" type="text" inputmode="decimal" v-model="numVal" :placeholder="numberPlaceholder" />
          <span v-if="cur.answer_kind === 'percent'" class="suffix">%</span>
          <span v-else-if="cur.answer_unit" class="suffix">{{ cur.answer_unit }}</span>
        </div>
        <input v-else-if="control === 'token'" class="cl-input mono" type="text" v-model="bankAnswer" :placeholder="tokenPlaceholder" />
        <textarea v-else class="cl-textarea" v-model="bankAnswer" :placeholder="answerPlaceholder" />

        <div class="d-actions">
          <CLButton :disabled="bankBusy || !bankAnswer.trim()" @click="judge">{{ bankBusy ? '判题中…' : '提交判题' }}</CLButton>
          <CLButton v-if="cur.has_hint && !bankHint" variant="ghost" :disabled="hintBusy" @click="revealHint">
            {{ hintBusy ? '取提示…' : '要一条提示' }}
          </CLButton>
          <span v-if="hintsUsed" class="hint-count">已用 {{ hintsUsed }} 次提示，本题掌握度会少涨一些</span>
        </div>
        <p v-if="bankHint" class="hint hint-shown">提示：{{ bankHint }}</p>

        <CLThinkBlock v-if="bankBusy && cur.verification_type !== 'unit_test'" :streaming="true" label="批改" />

        <div v-if="bankResult" class="result" :class="bankResult.passed ? 'ok' : 'bad'">
          <div class="r-head">
            <CLBadge :variant="bankResult.passed ? 'ok' : 'err'">{{ bankResult.passed === null ? '待人工核验' : (bankResult.passed ? '通过' : '未通过') }}</CLBadge>
            <CLBadge v-for="(n, i) in bankResult.error_tag_names" :key="i" variant="warn">{{ n }}</CLBadge>
          </div>
          <p v-if="bankResult.review_task" class="task-writeback">
            复习进度已更新：{{ bankResult.review_task.completed_count }}/{{ bankResult.review_task.target_count }} 题
            <span v-if="bankResult.review_task.completed">，本轮任务完成</span>
          </p>
          <div class="r-text"><MarkdownView :source="(bankResult.diagnosis || bankResult.details) || ''" /></div>
          <div v-if="bankResult.remediation" class="r-text"><b>下一步：</b><MarkdownView :source="bankResult.remediation" /></div>
          <p class="r-ref"><b>参考：</b>{{ bankResult.answer }}</p>
          <CLCitations v-if="bankResult.citations?.length" class="r-cites" :citations="bankResult.citations" />
        </div>
      </section>
      <section v-else class="cl-card empty-detail">从左侧选择一道题开始练习。</section>
    </div>

    <!-- ============ AI 生成 ============ -->
    <div v-else>
      <div class="cl-card controls">
        <label class="ctl grow"><span>知识点</span>
          <select class="cl-input" v-model="kp" @change="genKnowledgeId = 0; genErrorType = ''"><option v-for="k in KNOWLEDGE" :key="k" :value="k">{{ k }}</option></select>
        </label>
        <div class="ctl"><span>难度</span><CLSegmented v-model="difficulty" :options="diffOptions" /></div>
        <CLButton :disabled="genBusy" @click="generate">{{ genBusy ? '出题中…' : '生成练习' }}</CLButton>
      </div>
      <!-- 出完题后不收起：缩成一行灰字留在题目上面，说明这道题是怎么来的（检索了几段、出了几次） -->
      <p v-if="!genBusy && gen && genStages.length" class="gen-trail">
        {{ genStages.map((s) => s.label).join(' · ') }}
      </p>
      <div v-if="genBusy" class="cl-card gen-progress">
        <ol class="gen-stages">
          <li v-for="(st, k) in genStages" :key="k" :class="st.state">{{ st.label }}</li>
          <li v-if="!genStages.length" class="active">连接中</li>
        </ol>
        <div v-if="liveStem" class="gen-live">
          <span class="gen-live-label">题目正在写出来</span>
          <MarkdownView :source="liveStem" />
        </div>
      </div>
      <div v-if="gen" class="cl-card detail">
        <div v-if="gen.error" class="gen-error">
          <span>{{ gen.error }}</span>
          <span class="gen-error-actions">
            <CLButton variant="ghost" :disabled="genBusy" @click="generate">再生成一次</CLButton>
            <CLButton variant="text" @click="bankForGen">去做题库里的题</CLButton>
          </span>
        </div>
        <div v-if="gen.stem" class="d-head">
          <CLBadge variant="cite">{{ gen.type }}</CLBadge>
          <CLBadge>难度 {{ gen.difficulty }}</CLBadge>
          <CLBadge :variant="gen.verified ? 'ok' : 'warn'">{{ gen.verified ? '答案已验证' : (gen.verified === false ? '未通过验证' : '待人工核验') }}</CLBadge>
        </div>
        <div v-if="gen.stem" class="d-stem"><MarkdownView :source="splitChoiceOptions(gen.stem)" /></div>
        <p v-if="gen.hint" class="hint">提示：{{ gen.hint }}</p>
        <details v-if="gen.answer" class="ref-fold"><summary>查看参考答案</summary><div class="r-ref">{{ gen.answer }}</div></details>
      </div>
      <p v-else-if="!genBusy" class="empty">选择知识点与难度，点「生成练习」。系统会先自解验证答案再呈现。</p>
    </div>
  </div>
</template>

<style scoped>
.wrap { max-width: 1040px; margin: 0 auto; }
/* 从复习计划跳进来时的横幅：告诉人"这次作答会回写到那个任务上"。
   左侧竖线 + 靛蓝底，与 Solve 的题目条同一语言（都是"当前上下文"提示）。 */
.task-banner {
  display: flex; align-items: center; gap: 10px; padding: 10px 12px 10px 14px; margin-bottom: 14px;
  border: 1px solid var(--cl-tint-border); border-left: 3px solid var(--cl-brand);
  border-radius: var(--cl-radius-sm);
  background: var(--cl-tint-grad);
  color: var(--cl-text-2); font-size: 13px;
}
.cold { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }

/* ---------- 个性化推荐 ---------- */
.rec-grid { display: grid; grid-template-columns: 300px 1fr; gap: 18px; align-items: start; }
.weak-list { display: flex; flex-direction: column; gap: 8px; }
.rec-title { font-weight: 800; margin-bottom: 4px; letter-spacing: .2px; }
/* 薄弱点是一个单选列表，选中态必须比 hover 明显 —— 两者都只是"亮一点"的话，
   鼠标移开后就认不出选的是哪个了。选中额外给左侧竖条和辉光。 */
.weak {
  position: relative; width: 100%; padding: 11px 11px 11px 14px;
  border: 1px solid var(--cl-row-border); border-radius: var(--cl-radius-sm);
  background: var(--cl-row); color: var(--cl-text); text-align: left; cursor: pointer;
  transition: background .15s ease, border-color .15s ease, box-shadow .15s ease;
}
.weak::before {
  content: '';
  position: absolute; left: 0; top: 10px; bottom: 10px; width: 3px;
  border-radius: 0 3px 3px 0; background: var(--cl-accent-soft);
  transform: scaleX(0); transform-origin: left;
  transition: transform .16s ease;
}
.weak:hover { background: var(--cl-row-hover); border-color: var(--cl-row-border-hover); }
.weak.on {
  background: var(--cl-tint-grad);
  border-color: var(--cl-tint-border-strong);
  box-shadow: var(--cl-glow);
}
.weak.on::before { transform: scaleX(1); }
.weak:focus-visible { outline: 2px solid var(--cl-brand); outline-offset: 2px; }
.weak-head { display: flex; justify-content: space-between; gap: 8px; margin-bottom: 7px; font-size: 13px; }
.weak-head b { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.weak-head span { color: var(--cl-text-3); flex: none; }
.weak-tags { display: flex; gap: 6px; margin-top: 7px; flex-wrap: wrap; }
.rec-items { display: flex; flex-direction: column; gap: 12px; }
.rec-item { display: flex; flex-direction: column; gap: 10px; }
.rec-stem { line-height: 1.65; color: var(--cl-text-2); white-space: pre-line; }
.rec-reason { margin: 0; font-size: 12.5px; color: var(--cl-text-3); }
.rec-item :deep(.clb) { align-self: flex-start; }
.gen-progress { margin-top: 14px; }
.gen-trail { margin: 12px 0 -4px; font-size: 12px; color: var(--cl-text-3); }
.gen-stages { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.gen-stages li { position: relative; padding-left: 20px; font-size: 13px; color: var(--cl-text-3); }
.gen-stages li::before {
  content: ''; position: absolute; left: 4px; top: 50%; width: 8px; height: 8px; margin-top: -4px;
  border-radius: 50%; background: var(--cl-row-border);
}
.gen-stages li.done { color: var(--cl-text-2); }
.gen-stages li.done::before { background: var(--cl-accent); }
.gen-stages li.active { color: var(--cl-text); font-weight: 600; }
.gen-stages li.active::before { background: var(--cl-brand); animation: gen-pulse 1s ease-in-out infinite; }
.gen-stages li.warn { color: var(--cl-warn-fg); }
.gen-stages li.warn::before { background: var(--cl-warn); }
@keyframes gen-pulse { 50% { opacity: .35; } }
@media (prefers-reduced-motion: reduce) { .gen-stages li.active::before { animation: none; } }
.gen-live { margin-top: 12px; padding: 10px 12px; border-radius: var(--cl-radius-sm);
  background: var(--cl-row); border: 1px dashed var(--cl-row-border); font-size: 14px; }
.gen-live-label { display: block; font-size: 11.5px; color: var(--cl-text-3); margin-bottom: 4px; }
.gen-error-actions { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
.gen-error {
  padding: 10px 12px; border-radius: var(--cl-radius-sm);
  background: var(--cl-err-bg); color: var(--cl-err-fg);
  border: 1px solid var(--cl-err-bd);
}
/* ---------- 固定题库 ---------- */
.bank { display: grid; grid-template-columns: 320px 1fr; gap: 18px; align-items: start; }
.list-head { margin-bottom: 12px; }
/* 题量分布：一张小柱状图。压暗一档并单独描边，让它读起来是"侧栏里的一块图表"，
   而不是和下面的题目列表并列的又一组条目。 */
.dist {
  margin-bottom: 14px; padding: 10px 12px;
  border: 1px solid var(--cl-glass-border); border-radius: var(--cl-radius-sm);
  background: var(--cl-inset);
}
.dist-head { font-size: 12px; font-weight: 700; color: var(--cl-text-2); margin-bottom: 8px; }
.dist-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 5px; max-height: 30vh; overflow: auto; }
.dist-row { display: grid; grid-template-columns: 92px 1fr 26px; align-items: center; gap: 8px; cursor: pointer; }
.dist-name { font-size: 11.5px; color: var(--cl-text-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.dist-row:hover .dist-name { color: var(--cl-brand-600); }
/* 轨道不用 overflow:hidden：会把柱子的辉光裁掉。柱子自带圆角，不靠父级裁形状。 */
.dist-bar {
  height: 8px; border-radius: 4px;
  background: var(--cl-inset-deep);
  box-shadow: 0 0 0 1px var(--cl-glass-border) inset;
}
.dist-bar i {
  display: block; height: 100%; border-radius: 4px;
  background: linear-gradient(90deg, var(--cl-brand), var(--cl-accent));
  box-shadow: var(--cl-dot-glow-brand);
}
.dist-cnt { font-size: 11.5px; color: var(--cl-text-3); text-align: right; }

.prob-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; max-height: 62vh; overflow: auto; }
/* 题目列表是本页最长的滚动区（可能上百条），所以行样式要尽量便宜：
   只动 background / border-color，不做 transform 和阴影 —— 边滚边 hover 时，
   每一帧重算阴影的代价在长列表上是看得出来的。 */
.prob {
  padding: 10px 12px; border: 1px solid var(--cl-row-border); border-radius: var(--cl-radius-sm);
  background: var(--cl-row); cursor: pointer;
  transition: background .12s ease, border-color .12s ease;
}
.prob:hover { background: var(--cl-row-hover); border-color: var(--cl-row-border-hover); }
.prob.on {
  background: var(--cl-tint-grad);
  border-color: var(--cl-tint-border-strong);
}
.prob-top { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 5px; }
.prob-diff { font-size: 12px; color: var(--cl-text-3); flex: none; }
.prob-stem { font-size: 13px; color: var(--cl-text-2); line-height: 1.5; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.detail { display: flex; flex-direction: column; gap: 14px; }
.d-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.fn { font-size: 13px; color: var(--cl-text-2); }
.fn code { font-family: var(--cl-mono); background: var(--cl-code-inline-bg); color: var(--cl-brand-700); padding: 1px 6px; border-radius: 5px; }
/* 代码作答框走与 Markdown 代码块相同的底色，"写代码的地方"和"读代码的地方"统一 */
.code { font-family: var(--cl-mono); min-height: 160px; font-size: 13px; background: var(--cl-code-bg); }

/* ---------- 选项矩阵 ---------- */
/* ABCD 四个方块。深色下未选中态**不能**只靠边框 —— 一圈 1.5px 的暗线在氛围底上
   几乎消失，看起来像四个漂浮的字母。给一个浅浅的底把方块坐实。
   选中态用实心渐变而不是"底色变深"：多选题要一眼数出选了几个。 */
.choices { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.choice {
  width: 46px; height: 46px; border-radius: var(--cl-radius-sm); cursor: pointer;
  border: 1.5px solid var(--cl-row-border); background: var(--cl-row);
  font: inherit; font-size: 16px; font-weight: 700; color: var(--cl-text-2);
  transition: border-color .12s ease, background .12s ease, color .12s ease, box-shadow .12s ease;
}
.choice:hover { border-color: var(--cl-row-border-hover); background: var(--cl-row-hover); color: var(--cl-text); }
.choice.on {
  border-color: transparent; color: var(--cl-on-brand);
  background: var(--cl-brand-grad);
  box-shadow: var(--cl-glow);
}
.choice:focus-visible { outline: 2px solid var(--cl-brand); outline-offset: 2px; }
.choice-hint { font-size: 12.5px; color: var(--cl-text-3); }

/* 数值输入：后缀（% / 单位）贴着输入框，明确它是这个框的一部分而不是旁注 */
.num-input { display: flex; align-items: center; gap: 8px; }
.num-input .cl-input { width: 220px; font-family: var(--cl-mono); }
.num-input .suffix { font-size: 15px; font-weight: 700; color: var(--cl-accent-soft); }
.mono { font-family: var(--cl-mono); }

/* ---------- 判题结果 ---------- */
/* 通过/未通过是本页最重要的一次反馈，用左侧强调线 + 语义底色 + 辉光三重标记，
   滚动到长题目下面时也能一眼认出结果块。
   .bad 用琥珀而不是红：答错是学习过程的正常一步，不是错误状态。 */
.result {
  padding: 13px 16px 13px 15px; border-radius: var(--cl-radius-sm);
  border: 1px solid var(--cl-row-border); border-left: 3px solid var(--cl-border-strong);
}
.result.ok {
  background: linear-gradient(90deg, var(--cl-ok-tint), var(--cl-inset));
  border-color: var(--cl-ok-bd); border-left-color: var(--cl-ok);
  box-shadow: var(--cl-edge-glow-ok);
}
.result.bad {
  background: linear-gradient(90deg, var(--cl-warn-tint), var(--cl-inset));
  border-color: var(--cl-warn-bd); border-left-color: var(--cl-warn);
  box-shadow: var(--cl-edge-glow-warn);
}
.r-head { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.r-text { margin: 6px 0 0; font-size: 14px; line-height: 1.6; }
.r-ref { margin: 10px 0 0; padding-top: 10px; border-top: 1px dashed var(--cl-row-border); font-size: 13px; color: var(--cl-text-2); }
.task-writeback { margin: 8px 0 0; font-size: 13px; font-weight: 700; color: var(--cl-accent-soft); }
.empty, .empty-detail { color: var(--cl-text-3); font-size: 14px; }
.empty-detail { display: grid; place-items: center; min-height: 200px; }
.controls { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 16px; margin-bottom: 16px; }
.ctl { display: flex; flex-direction: column; gap: 7px; font-size: 12.5px; font-weight: 600; color: var(--cl-text-2); }
.ctl.grow { flex: 1; min-width: 200px; }
.hint { font-size: 13px; color: var(--cl-text-3); margin: 0; }
.d-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.hint-count { font-size: 12.5px; color: var(--cl-warn-fg); }
/* 提示是"另一种介质"，压暗 + 左侧琥珀竖条，与结果块的语义色区分开 */
.hint-shown {
  margin: 12px 0 0; padding: 10px 12px;
  border-left: 3px solid var(--cl-warn);
  border-radius: var(--cl-radius-sm);
  background: var(--cl-inset);
  box-shadow: var(--cl-edge-glow-warn);
  color: var(--cl-text-2);
}
.gen-think { margin-bottom: 16px; }
/* 参考答案默认折叠：先自己想，点开才看。展开区压暗一档与题面区分。 */
.ref-fold { font-size: 14px; }
.ref-fold summary {
  cursor: pointer; color: var(--cl-brand-600); font-weight: 600;
  padding: 6px 0; list-style: none;
}
.ref-fold summary::-webkit-details-marker { display: none; }
.ref-fold summary::before { content: '▸ '; display: inline-block; transition: transform .16s ease; }
.ref-fold[open] summary::before { content: '▾ '; }
.ref-fold summary:hover { color: var(--cl-brand-700); }
.ref-fold[open] .r-ref {
  margin-top: 6px; padding: 10px 12px; border-top: 0;
  border: 1px solid var(--cl-glass-border); border-radius: var(--cl-radius-sm);
  background: var(--cl-inset);
}
@media (max-width: 860px) { .bank, .rec-grid { grid-template-columns: 1fr; } }
@media (max-width: 560px) { .num-input .cl-input { width: 100%; } }

@media (prefers-reduced-motion: reduce) {
  .weak, .prob, .choice { transition: none; }
}

/* 题目出处：低调一行，不能盖过题干。它是"这题出自哪"，不是答案线索 */
.d-origin {
  margin: -4px 0 14px; display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
  font-size: 12px; color: var(--cl-text-3); line-height: 1.6;
}
.origin-tag {
  padding: 1px 8px; border-radius: var(--cl-radius-pill);
  border: 1px solid var(--cl-cite-bd); background: var(--cl-cite-bg); color: var(--cl-cite-fg);
  font-weight: 600;
}
.origin-item { color: var(--cl-text-2); }
.origin-item + .origin-item::before { content: "·"; margin-right: 8px; color: var(--cl-text-4); }
</style>
