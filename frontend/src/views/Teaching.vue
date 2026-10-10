<script setup lang="ts">
/**
 * 助教工作台：班级学情 / 掌握度热力图 / 作业下发与验收 / 复核 AI 判定。
 *
 * 原来这页只有四个总计数和一张全局错因排行 —— 能回答"班里在发生什么"，
 * 回答不了"谁在哪一章卡住了"，而后者才是助教要做的事。三条渲染纪律：
 *
 * 1) **未评估不是 0 分**。热力图里没有掌握度记录的格子画成留白，不给它一个
 *    "很低"的颜色。0 和"没测过"在色阶上分不开，但对助教意味着相反的两件事
 *    （教了没学会 / 还没教到）。覆盖率单独标出来。
 * 2) **掉队的排前面**。行按平均掌握度升序，完全没数据的单独垫底 ——
 *    那是"还没开始"，跟"学了但弱"不该混在一起排。
 * 3) **完成度是算出来的，不是登记的**。作业验收直接数下发之后的正确作答，
 *    页面上写明这一点，免得助教以为学生需要手动"提交作业"。
 */
import { ref, onActivated, computed, watch } from 'vue'
import { download, get, post } from '@/api/client'
import { toast } from '@/api/toast'
import { useCourseStore } from '@/stores/course'
import CLPageHeader from '@/components/ui/CLPageHeader.vue'
import CLBadge from '@/components/ui/CLBadge.vue'
import CLButton from '@/components/ui/CLButton.vue'
import CLCard from '@/components/ui/CLCard.vue'
import CLField from '@/components/ui/CLField.vue'
import CLProgress from '@/components/ui/CLProgress.vue'
import CLSegmented from '@/components/ui/CLSegmented.vue'
import CLSelect from '@/components/ui/CLSelect.vue'
import CLSkeleton from '@/components/ui/CLSkeleton.vue'
import CLEmpty from '@/components/ui/CLEmpty.vue'
import CLStat from '@/components/ui/CLStat.vue'
import MarkdownView from '@/components/MarkdownView.vue'
import ReviewContext from '@/components/ReviewContext.vue'
import { coverageText, masteryPct, type MasterySummary } from '@/utils/mastery'
import { useThemeStore } from '@/stores/theme'

interface Point { knowledge_id: number; name: string; chapter: string
  learners: number; avg_mastery: number; error_open: number; error_total: number }
interface Cell { mastery: number; state: string }
interface Row { username: string; name: string; class_name: string; student_id: string
  cells: Record<string, Cell>; covered: number; avg_mastery: number | null
  attempts: number; correct_n: number; last_at: string }
interface Heatmap {
  course_display: string; class_name: string; classes: string[]
  points: Point[]; students: Row[]
  point_total: number; point_shown: number; student_total: number
  common_errors: { error_type: string; error_name: string; count: number }[]
}
interface Assignment {
  id: number; title: string; note: string; course_id: string; status: string
  due_date: string; class_name: string; target_count: number; created_at: string
  overdue: boolean; points: { knowledge_id: number; name: string }[]
  students: { username: string; name: string; done: number; target: number
    rate: number; completed: boolean; points_done: number; points_total: number }[]
  completed_students: number; total_students: number; completion_rate: number
  // 这批知识点的来源错因，以及该错因对应的处置动作（后端 classroom.assignment_view 翻好）。
  // 空串 = 助教手动挑的，不是没取到。
  source_name: string; source_follow_up: string
  // 下发前后的变化（classroom.assignment_view → db.assignment_effect）
  effect?: { pairs: number; before: number | null; after: number | null
    attempts_after: number; accuracy_after: number | null; students_active: number; enough: boolean }
}

const courseStore = useCourseStore()
// 默认落在「今日」：助教进来要知道今天先做什么，而不是先看四个总数（docs/35 P1-6）
const tab = ref('today')
const tabs = [
  { label: '今日', value: 'today' },
  { label: '班级总览', value: 'overview' },
  { label: '复核队列', value: 'queue' },
  { label: '掌握度热力图', value: 'heatmap' },
  { label: '作业', value: 'assignment' },
]


const data = ref<any>(null)
const loading = ref(true)
interface StudentDetail {
  profile: { username: string; name: string; student_id: string; class_name: string }
  activity: { attempts: number; correct_n: number; last_at: string }
  mastery: { knowledge_id: number; knowledge_name: string; mastery: number }[]
  mastery_summary: MasterySummary
  errors: {
    id: number; error_name: string; knowledge: string; diagnosis: string
    resolved: boolean; ta_reviewed: boolean; ta_note: string; created_at: string
  }[]
  placement: { level: number; level_name: string; finished_at: string } | null
  review: { completion_rate: number; completed_tasks: number; total_tasks: number } | null
}

const heat = ref<Heatmap | null>(null)
const heatLoading = ref(false)
const classFilter = ref('')
const assignments = ref<Assignment[]>([])
const points = ref<{ id: number; name: string; chapter: string }[]>([])
// 班级下拉的唯一数据源。两个 tab 各自加载各自的数据，谁先打开都要能选到班。
const classes = ref<string[]>([])
const openAssignment = ref<number | null>(null)

const maxCount = computed(() => Math.max(1, ...(data.value?.common_errors || []).map((c: any) => c.count)))

// 总览的作用域。此前四个数是全库 COUNT —— 切到"计算机网络"看到的仍是四门之和，
// 而"活跃学生"是历史上做过任何一题的人，开课两个月后永远满格。
const ovScope = ref('course')     // course = 只看当前课程；all = 全体
const ovClass = ref('')

async function loadOverview() {
  loading.value = true
  try {
    const q = new URLSearchParams()
    if (ovScope.value === 'course') q.set('course', courseStore.current)
    if (ovClass.value) q.set('class_name', ovClass.value)
    const qs = q.toString()
    data.value = await get(`/teaching/overview${qs ? `?${qs}` : ''}`)
    if (data.value?.classes?.length) classes.value = data.value.classes
  } finally { loading.value = false }
}
watch([ovScope, ovClass], () => {
  if (tab.value === 'overview') loadOverview()
  else if (tab.value === 'today') loadToday()
})

// ---- 复核队列 ----
// 此前只能复核总览里那固定 15 条（recent_errors 的 SQL 一个 WHERE 都没有），
// 想系统性复核一个班的错题做不到。
const queue = ref<{ items: any[]; total: number; offset: number; limit: number } | null>(null)
const queueLoading = ref(false)
const qReviewed = ref('no')       // 默认只看未复核的：那才是"待办"
const QUEUE_FILTERS = [
  { label: '待复核', value: 'no' }, { label: '已复核', value: 'yes' }, { label: '全部', value: '' },
]
// 入队原因（docs/35 P1-3）。待复核默认按"学生申请 → 验证未通过 → 无依据 → 粗心 → 普通"排序
const qReason = ref('')
const REASON_OPTIONS = [
  { label: '全部原因', value: '' },
  { label: '学生申请复核', value: 'student_request' },
  { label: '验证未通过', value: 'verify_failed' },
  { label: '未检索到教材依据', value: 'no_evidence' },
  { label: '判为粗心', value: 'careless' },
  { label: '普通错题', value: 'manual' },
]

// 学生对大厅问答的复核申请（docs/35 P1-2）。它们不是错题，单独一张表，
// 但在助教这里和错题复核放在同一个 tab：对助教来说都是"有人在等我看一眼"。
const qaRequests = ref<any[]>([])
const qaTotal = ref(0)
const replyFor = ref(0)
const replyVerdict = ref('agree')
const replyText = ref('')
async function loadQaRequests() {
  try {
    const q = new URLSearchParams({ status: 'open', limit: '20' })
    if (ovScope.value === 'course') q.set('course', courseStore.current)
    const r = await get<{ items: any[]; total: number }>('/teaching/review-requests?' + q.toString())
    qaRequests.value = r.items
    qaTotal.value = r.total
  } catch { qaRequests.value = []; qaTotal.value = 0 }
}
function startReply(id: number) { replyFor.value = id; replyVerdict.value = 'agree'; replyText.value = '' }
async function sendReply(item: any) {
  submitting.value = true
  try {
    await post('/teaching/review-requests/reply',
      { request_id: item.id, verdict: replyVerdict.value, reply: replyText.value })
    qaRequests.value = qaRequests.value.filter((x) => x.id !== item.id)
    qaTotal.value = Math.max(0, qaTotal.value - 1)
    replyFor.value = 0
    toast('已回复，学生在「错题与学习记录」里能看到')
  } finally { submitting.value = false }
}

async function loadQueue(offset = 0) {
  queueLoading.value = true
  try {
    const q = new URLSearchParams({ limit: '20', offset: String(offset) })
    if (ovScope.value === 'course') q.set('course', courseStore.current)
    if (ovClass.value) q.set('class_name', ovClass.value)
    if (qReviewed.value) q.set('reviewed', qReviewed.value)
    if (qReason.value) q.set('reason', qReason.value)
    queue.value = await get('/teaching/errors?' + q.toString())
    if (qReviewed.value !== 'yes' && (!qReason.value || qReason.value === 'student_request')) await loadQaRequests()
    else qaRequests.value = []
  } catch {
    toast('复核队列读取失败', 'error')
  } finally {
    queueLoading.value = false
  }
}
watch([qReviewed, qReason, ovScope, ovClass], () => { if (tab.value === 'queue') loadQueue(0) })

async function loadHeatmap() {
  heatLoading.value = true
  try {
    const q = new URLSearchParams({ course: courseStore.current })
    if (classFilter.value) q.set('class_name', classFilter.value)
    heat.value = await get<Heatmap>('/teaching/heatmap?' + q.toString())
    // 两个 tab 都往同一个 classes 里写：先开哪个都能在另一个里选到班。
    // 按班筛过之后 heat.classes 仍是全量（后端不受 class_name 影响），所以不会越筛越少。
    if (heat.value?.classes?.length) classes.value = heat.value.classes
  } finally {
    heatLoading.value = false
  }
}

async function loadAssignments() {
  const [list, kp] = await Promise.all([
    get<{ items: Assignment[]; classes: string[] }>('/teaching/assignments?course=' + encodeURIComponent(courseStore.current)),
    get<{ items: { id: number; name: string; chapter: string }[] }>('/knowledge?course=' + encodeURIComponent(courseStore.current)),
  ])
  assignments.value = list.items
  points.value = kp.items
  // 班级列表接口本来就返回了，此前被丢掉，下发表单转而去读 heat?.classes ——
  // 而 heat 只在打开热力图那个 tab 时才加载。症状是：直接进「作业」tab，
  // 班级下拉里只有"全体"一项，除非先去点一下热力图再回来。
  classes.value = list.classes || []
}

// 学生详情抽屉。缓存按 (学生, 课程) 存：助教在一屏里会反复点开同几个人，
// 每次都重新拉一遍既慢又白费 —— 但换课程要重拉，否则会拿另一门课的画像。
const detailFor = ref('')
const detailLoading = ref(false)
const detail = ref<StudentDetail | null>(null)
const detailCache = new Map<string, StudentDetail>()

async function openStudent(username: string) {
  detailFor.value = username
  const key = `${username}::${courseStore.current}`
  const hit = detailCache.get(key)
  detail.value = hit || null
  if (hit) return
  detailLoading.value = true
  try {
    const d = await get<StudentDetail>(
      `/teaching/student/${encodeURIComponent(username)}?course=${encodeURIComponent(courseStore.current)}`)
    detailCache.set(key, d)
    // 抽屉可能已经被关掉或换了人：请求回来时再对一次，避免把上一个人的数据画上去
    if (detailFor.value === username) detail.value = d
  } catch {
    toast('读取学生详情失败', 'error')
    detailFor.value = ''
  } finally {
    detailLoading.value = false
  }
}

// 导出。三种口径都跟着当前的课程与班级筛选走 —— 导出的必须是"我现在看到的这一屏"，
// 否则老师筛了一个班、导出来却是全体，而这件事在表格打开之前看不出来。
const exporting = ref('')
async function exportCsv(kind: 'mastery' | 'errors' | 'assignment', assignmentId = 0) {
  if (exporting.value) return
  exporting.value = kind
  try {
    const q = new URLSearchParams({ kind, course: courseStore.current })
    if (kind !== 'assignment' && classFilter.value) q.set('class_name', classFilter.value)
    if (assignmentId) q.set('assignment_id', String(assignmentId))
    await download('/teaching/export?' + q.toString(), `CourseLoop-${kind}.csv`)
  } catch {
    toast('导出失败', 'error')
  } finally {
    exporting.value = ''
  }
}

// ---- 今日 ----
const todayData = ref<any>(null)
const todayLoading = ref(false)
async function loadToday() {
  todayLoading.value = true
  try {
    const q = new URLSearchParams({ course: courseStore.current })
    if (ovClass.value) q.set('class_name', ovClass.value)
    todayData.value = await get('/teaching/today?' + q.toString())
  } finally { todayLoading.value = false }
}
/** 主行动与摘要卡的跳转：都落到已有的 tab 或抽屉里，不另起页面 */
function act(p: any) {
  if (!p) return
  if (p.kind === 'student' && p.username) { openStudent(p.username); return }
  if (p.kind === 'review') { qReviewed.value = 'no'; qReason.value = p.reason || ''; tab.value = 'queue'; return }
  if (p.kind === 'assignment') { tab.value = 'assignment'; openAssignment.value = p.assignment_id || null; return }
  if (p.kind === 'intervene') { tab.value = 'assignment'; form.value.source = p.source || ''; return }
}
function pctText(v: number | null | undefined) { return v === null || v === undefined ? '—' : `${Math.round(v * 100)}%` }

async function loadTab() {
  if (tab.value === 'today') await loadToday()
  else if (tab.value === 'overview') await loadOverview()
  else if (tab.value === 'queue') await loadQueue(0)
  else if (tab.value === 'heatmap') await loadHeatmap()
  else if (tab.value === 'assignment') await loadAssignments()
}
watch(tab, loadTab)
watch(() => courseStore.current, loadTab)
watch(classFilter, () => { if (tab.value === 'heatmap') loadHeatmap() })

onActivated(async () => {
  await courseStore.load()
  // 总览数据里带着错因码表与班级列表，改判下拉和下发表单都要用，所以先取一次
  if (!data.value) await loadOverview()
  await loadTab()
})

/* ---------- 热力图着色（测试报告 I-06） ---------- */
// 原先是"红→橙→黄→蓝→青"的多色相 + 低透明度：粉、橙、黄几乎分不开，色弱更分不开，
// 而且格子里没有字，数值只在鼠标悬停时才看得到。现在三层冗余：
//   1. 格子里直接写百分数（读屏另有完整的 aria-label）；
//   2. 单色相亮度梯度，与知识图谱同一组 --cl-map-ramp-1..5（三档主题各一组），
//      低掌握度是最深、最显眼的那档 —— 助教要找的正是它们；
//   3. 等级与学生端状态同一组阈值（mastery.derive_state）：<55% 薄弱、55–80% 一般、≥80% 掌握，
//      薄弱格再加一个角标，不靠颜色也能扫出来。
// 字色按实际底色亮度挑黑/白：同一档在日间和夜间的亮度不同，写死会有一档对比度不够。
const LEVELS = [
  { key: 'weak', label: '薄弱', range: '<55%', min: 0 },
  { key: 'mid', label: '一般', range: '55–80%', min: 0.55 },
  { key: 'good', label: '掌握', range: '≥80%', min: 0.8 },
] as const
function levelOf(m: number) {
  return [...LEVELS].reverse().find((l) => m >= l.min) || LEVELS[0]
}
const RAMP_FALLBACK = ['#4338ca', '#5b5ef0', '#818cf8', '#a5b4fc', '#e0e7ff']
const ramp = ref<{ bg: string; ink: string }[]>([])
function luminance(hex: string): number {
  const m = hex.replace('#', '').match(/^([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i)
  if (!m) return 0
  const [r, g, b] = m.slice(1).map((h) => {
    const c = parseInt(h, 16) / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}
function readRamp() {
  const css = getComputedStyle(document.documentElement)
  ramp.value = RAMP_FALLBACK.map((fb, i) => {
    const bg = css.getPropertyValue(`--cl-map-ramp-${i + 1}`).trim() || fb
    // 白字对比度 vs 深字对比度，取大的那个
    const L = luminance(bg)
    return { bg, ink: (1.05 / (L + 0.05)) >= ((L + 0.05) / 0.06) ? '#ffffff' : '#1e1b4b' }
  })
}
const themeStore = useThemeStore()
watch(() => themeStore.mode, () => requestAnimationFrame(readRamp))
readRamp()
function cellStyle(m: number) {
  const r = ramp.value.length ? ramp.value : RAMP_FALLBACK.map((bg) => ({ bg, ink: '#fff' }))
  const step = r[Math.min(r.length - 1, Math.max(0, Math.floor(m * r.length)))]
  return { background: step.bg, color: step.ink }
}
function cellLabel(student: string, point: string, m: number) {
  return `${student}，${point}，掌握度 ${Math.round(m * 100)}%，${levelOf(m).label}`
}
function pct(v: number | null): string {
  return v === null ? '—' : `${Math.round(v * 100)}%`
}

/* ---------- 作业下发 ---------- */
// source = 这批知识点是被哪个班级共性错因挑出来的。选项直接取自本班已聚合的错因
// （data.common_errors），而不是整张码表 —— 只有真的出现过的错因才谈得上是"来源"。
const form = ref({ title: '', note: '', target: '3', due: '', klass: '', source: '' })
const sourceOptions = computed(() => (data.value?.common_errors || [])
  .map((c: any) => ({ code: c.error_type, label: `${c.error_name}（${c.count} 条）` })))
const picked = ref<number[]>([])
const publishing = ref(false)
// 按章分组的多选。没用 CLSelect：那是单选组件，而下发作业一次要挑好几个点，
// 逐个选再逐个确认太啰嗦；章节分组的芯片墙能让人直接按"这一章"整片选。
// 知识点搜索（I-08）：一门课三四十个点，在芯片墙里逐个找太慢。按名称或章节名过滤，
// 命中章节名时整章都留着。
const pointQuery = ref('')
const pointGroups = computed(() => {
  const q = pointQuery.value.trim().toLowerCase()
  const byChapter = new Map<string, { id: number; name: string }[]>()
  for (const p of points.value) {
    if (q && !p.name.toLowerCase().includes(q) && !(p.chapter || '').toLowerCase().includes(q)) continue
    const key = p.chapter || '未归章'
    if (!byChapter.has(key)) byChapter.set(key, [])
    byChapter.get(key)!.push({ id: p.id, name: p.name })
  }
  return [...byChapter].map(([chapter, items]) => ({ chapter, items }))
})
// 下发前的完整摘要（I-08）：选了哪些点、给谁、做几道、何时截止，一句话写全。
// 芯片墙一滚动就看不到自己选了什么，窄屏下尤其如此 —— 点"下发"之前得能核对一遍。
const pickedNames = computed(() => {
  const byId = new Map(points.value.map((p: any) => [p.id, p.name]))
  return picked.value.map((id) => byId.get(id) || `#${id}`)
})
const publishSummary = computed(() => {
  const who = form.value.klass ? `「${form.value.klass}」` : '全体学生'
  const due = form.value.due ? `，${form.value.due.slice(5).replace('-', '/')} 截止` : '，不设截止日期'
  return `将给${who}下发 ${picked.value.length} 个知识点，每个点做对 ${Number(form.value.target) || 3} 道${due}。`
})
function togglePoint(id: number) {
  const i = picked.value.indexOf(id)
  if (i >= 0) picked.value.splice(i, 1)
  else picked.value.push(id)
}
function toggleChapter(items: { id: number }[]) {
  const ids = items.map((i) => i.id)
  const allOn = ids.every((id) => picked.value.includes(id))
  picked.value = allOn
    ? picked.value.filter((id) => !ids.includes(id))
    : [...new Set([...picked.value, ...ids])]
}

async function publish() {
  const ids = picked.value
  if (!form.value.title.trim()) { toast('先给作业起个标题', 'error'); return }
  if (!ids.length) { toast('至少选一个知识点', 'error'); return }
  publishing.value = true
  try {
    await post('/teaching/assignments', {
      course: courseStore.current, title: form.value.title.trim(), note: form.value.note.trim(),
      knowledge_ids: ids, target_count: Number(form.value.target) || 3,
      class_name: form.value.klass.trim(), due_date: form.value.due.trim(),
      source_error_type: form.value.source,
    })
    form.value = { title: '', note: '', target: '3', due: '', klass: '', source: '' }
    picked.value = []
    await loadAssignments()
    toast('已下发')
  } finally {
    publishing.value = false
  }
}

async function close(a: Assignment) {
  if (!window.confirm(`关闭「${a.title}」？学生那边不再显示，助教侧仍可回看完成情况。`)) return
  await post('/teaching/assignments/close', { assignment_id: a.id })
  await loadAssignments()
}

/* ---------- 复核（原有逻辑） ---------- */
const tagGroups = computed(() => [{ label: '错因类型', options: (data.value?.error_tags || []).map((t: any) => t.name) }])
function codeOfName(name: string): string {
  return (data.value?.error_tags || []).find((t: any) => t.name === name)?.code || ''
}

const openId = ref<number | null>(null)
const pickName = ref('')
const note = ref('')
const submitting = ref(false)

function startOverride(id: number) { openId.value = id; pickName.value = ''; note.value = '' }
function cancelOverride() { openId.value = null }

async function confirmRecord(r: any) {
  submitting.value = true
  try {
    const res: any = await post('/teaching/review-error', { record_id: r.id, verdict: 'confirm' })
    Object.assign(r, { ta_reviewed: res.ta_reviewed, ta_verdict: res.ta_verdict })
    toast('已确认该判定')
  } finally { submitting.value = false }
}

async function submitOverride(r: any) {
  const code = codeOfName(pickName.value)
  if (!code) { toast('请先选择纠正后的错因类型', 'error'); return }
  submitting.value = true
  try {
    const res: any = await post('/teaching/review-error', {
      record_id: r.id, verdict: 'override', error_type: code, note: note.value,
    })
    Object.assign(r, {
      ta_reviewed: res.ta_reviewed, ta_verdict: res.ta_verdict,
      ai_error_name: r.error_name, error_type: res.error_type, error_name: res.error_name,
      follow_up: res.follow_up || '', impact: res.impact || [],
    })
    openId.value = null
    toast('已改判并回写记录')
  } finally { submitting.value = false }
}
</script>

<template>
  <div class="wrap">
    <CLPageHeader title="助教工作台" sub="今天先处理什么、班级学情、复核与作业">
      <template #actions><CLSegmented v-model="tab" :options="tabs" /></template>
    </CLPageHeader>

    <!-- ============ 今日 ============ -->
    <template v-if="tab === 'today'">
      <div class="scope-bar">
        <CLBadge variant="cite">{{ courseStore.info?.short }}</CLBadge>
        <select class="cl-input klass" v-model="ovClass">
          <option value="">全部班级</option>
          <option v-for="k in classes" :key="k" :value="k">{{ k }}</option>
        </select>
      </div>
      <CLSkeleton v-if="todayLoading && !todayData" :rows="5" />
      <template v-else-if="todayData">
        <CLCard class="today-focus" :class="{ calm: todayData.primary.kind === 'none' }">
          <div class="eyebrow">今天先做这件事</div>
          <h2>{{ todayData.primary.title }}</h2>
          <p>{{ todayData.primary.why }}</p>
          <CLButton v-if="todayData.primary.button" @click="act(todayData.primary)">{{ todayData.primary.button }}</CLButton>
        </CLCard>

        <div class="today-grid">
          <CLCard title="待复核" :sub="`共 ${todayData.review.total} 条`">
            <ul class="today-list">
              <li><button class="row-link" @click="act({ kind: 'review', reason: 'student_request' })">
                <span>学生申请复核</span><b :class="{ hot: todayData.review.student_waiting }">{{ todayData.review.student_waiting }}</b></button></li>
              <li><button class="row-link" @click="act({ kind: 'review', reason: 'verify_failed' })">
                <span>验证未通过</span><b>{{ todayData.review.by_reason.verify_failed || 0 }}</b></button></li>
              <li><button class="row-link" @click="act({ kind: 'review', reason: 'no_evidence' })">
                <span>未检索到教材依据</span><b>{{ todayData.review.by_reason.no_evidence || 0 }}</b></button></li>
              <li><button class="row-link" @click="act({ kind: 'review', reason: 'careless' })">
                <span>判为粗心（需核实）</span><b>{{ todayData.review.by_reason.careless || 0 }}</b></button></li>
              <li><button class="row-link" @click="act({ kind: 'review', reason: 'manual' })">
                <span>普通错题</span><b>{{ todayData.review.by_reason.manual || 0 }}</b></button></li>
            </ul>
          </CLCard>

          <CLCard title="作业" :sub="`${todayData.assignments.open} 份进行中 · ${todayData.assignments.overdue} 份逾期`">
            <CLEmpty v-if="!todayData.assignments.items.length" title="没有进行中的作业"
              :hint="todayData.uncovered_error ? `可以针对「${todayData.uncovered_error.error_name}」下发一份` : '需要时在「作业」里下发'">
              <CLButton v-if="todayData.uncovered_error" variant="ghost"
                @click="act({ kind: 'intervene', source: todayData.uncovered_error.error_type })">去下发</CLButton>
            </CLEmpty>
            <ul v-else class="today-list">
              <li v-for="a in todayData.assignments.items" :key="a.id">
                <button class="row-link" @click="act({ kind: 'assignment', assignment_id: a.id })">
                  <span>{{ a.title }}<CLBadge v-if="a.overdue" variant="err">逾期</CLBadge></span>
                  <b>{{ a.completed_students }}/{{ a.total_students }}</b>
                </button>
              </li>
            </ul>
          </CLCard>

          <CLCard title="需要留意的学生" sub="平均掌握度偏低，或一周没有作答">
            <CLEmpty v-if="!todayData.risk_students.length" title="暂时没有" hint="全班都在正常推进。" />
            <ul v-else class="today-list">
              <li v-for="r in todayData.risk_students" :key="r.username">
                <button class="row-link" @click="openStudent(r.username)">
                  <span>{{ r.name }} <small>{{ r.why }}</small></span><b>{{ pctText(r.avg_mastery) }}</b>
                </button>
              </li>
            </ul>
          </CLCard>
        </div>
      </template>
    </template>

    <CLSkeleton v-else-if="loading" :rows="4" />

    <!-- ============ 班级总览 ============ -->
    <template v-else-if="tab === 'overview' && data">
      <!-- 作用域条。四个数按它算 —— 不说清楚算的是哪一门、哪个班，
           这四个数就是四个没有意义的大数字 -->
      <div class="scope-bar">
        <CLSegmented v-model="ovScope"
          :options="[{ label: courseStore.info?.short || '当前课程', value: 'course' },
                     { label: '全部课程', value: 'all' }]" />
        <select class="cl-input klass" v-model="ovClass">
          <option value="">全部班级</option>
          <option v-for="k in classes" :key="k" :value="k">{{ k }}</option>
        </select>
      </div>
      <div class="tiles">
        <CLStat accent :value="data.stats.students" label="学生数" />
        <CLStat :value="data.stats.active_students"
          :label="`近 ${data.stats.active_days} 天活跃`" />
        <!-- 作答与错题是累计数，活跃人数是近 N 天：时间范围不同就写在标签上（I-03） -->
        <CLStat :value="data.stats.attempts" label="累计作答次数" />
        <CLStat :value="data.stats.errors" label="累计错题数" />
      </div>

      <div class="cols">
        <CLCard title="班级共性错因" sub="统计的是改判后的判定，不是 AI 原判">
          <CLEmpty v-if="!data.common_errors.length" title="暂无错因数据" hint="学生作答后这里会聚合高频错因" />
          <ul v-else class="ce-list">
            <li v-for="c in data.common_errors" :key="c.error_type">
              <CLProgress :value="c.count / maxCount * 100" :label="c.error_name" :right="String(c.count)" />
            </li>
          </ul>
        </CLCard>

        <CLCard title="最近错题" sub="复核结果会用于改进后续的 AI 判定">
          <CLEmpty v-if="!data.recent.length" title="还没有错题记录"
            hint="学生在解题或练习中答错后，错题会自动出现在这里等你复核。" />
          <ul v-else class="rc-list">
            <li v-for="r in data.recent" :key="r.id" class="rc-item" :class="{ reviewed: r.ta_reviewed }">
              <div class="rc-top">
                <button class="s-open inline" @click="openStudent(r.username)">{{ r.username }}</button>
                <span class="rc-at">{{ (r.created_at || '').slice(5, 16) }}</span>
              </div>
              <ReviewContext :r="r" compact />

              <div v-if="r.ta_reviewed" class="rc-reviewed">
                <CLBadge :variant="r.ta_verdict === 'override' ? 'ok' : 'neutral'">
                  {{ r.ta_verdict === 'override' ? '已改判' : '已确认' }}
                </CLBadge>
                <span v-if="r.ta_note" class="rc-note">{{ r.ta_note }}</span>
              </div>
              <ul v-if="r.impact?.length" class="rc-impact">
                <li v-for="(x, k) in r.impact" :key="k">{{ x }}</li>
              </ul>
              <div v-else-if="openId === r.id" class="rc-override">
                <CLSelect v-model="pickName" :groups="tagGroups" :allow-create="false" placeholder="选择纠正后的错因" />
                <CLField v-model="note" textarea :rows="2" placeholder="给学生/给自己的备注（可选）" />
                <div class="rc-btns">
                  <CLButton variant="primary" :disabled="submitting" @click="submitOverride(r)">提交改判</CLButton>
                  <CLButton variant="text" @click="cancelOverride">取消</CLButton>
                </div>
              </div>
              <div v-else class="rc-btns rc-actions">
                <CLButton variant="ghost" :disabled="submitting" @click="confirmRecord(r)">确认判定</CLButton>
                <CLButton variant="text" :disabled="submitting" @click="startOverride(r.id)">改判</CLButton>
              </div>
            </li>
          </ul>
        </CLCard>
      </div>
    </template>

    <!-- ============ 热力图 ============ -->
    <template v-else-if="tab === 'heatmap'">
      <CLSkeleton v-if="heatLoading" :rows="6" />
      <template v-else-if="heat">
        <div class="heat-bar">
          <CLBadge variant="cite">{{ heat.course_display }}</CLBadge>
          <select class="cl-input klass" v-model="classFilter">
            <option value="">全部班级</option>
            <option v-for="k in heat.classes" :key="k" :value="k">{{ k }}</option>
          </select>
          <span class="thin-note">
            显示 {{ heat.point_shown }} / {{ heat.point_total }} 个知识点（只画有人练过或错过的），
            {{ heat.students.length }} / {{ heat.student_total }} 名学生
          </span>
          <!-- 图例写出区间，不只是"弱 → 强"：颜色读不出来的人照样知道阈值 -->
          <span class="legend" aria-label="图例：格内数字为掌握度百分比">
            <i v-for="m in [0.1, 0.3, 0.5, 0.7, 0.9]" :key="m" :style="cellStyle(m)" aria-hidden="true" />
            <b>0% → 100%</b>
            <b class="legend-levels">{{ LEVELS.map((l) => `${l.label} ${l.range}`).join(' · ') }}</b>
            <i class="weak-mark" aria-hidden="true" /><b>薄弱</b>
            <i class="empty" aria-hidden="true" /><b>未评估</b>
          </span>
          <!-- 导出跟着上面的班级筛选走。屏上截断了 60 人 / 40 个知识点，
               导出不截断 —— 老师导出来正是为了看全，这一点写在按钮的 title 里 -->
          <span class="exports">
            <CLButton variant="ghost" :disabled="!!exporting"
              title="导出当前筛选下的完整矩阵（不受屏上 60 人 / 40 点的截断限制）"
              @click="exportCsv('mastery')">
              {{ exporting === 'mastery' ? '导出中…' : '导出掌握度' }}
            </CLButton>
            <CLButton variant="ghost" :disabled="!!exporting" @click="exportCsv('errors')">
              {{ exporting === 'errors' ? '导出中…' : '导出错题清单' }}
            </CLButton>
          </span>
        </div>

        <CLCard v-if="!heat.points.length">
          <CLEmpty title="还没有可画的数据"
            hint="学生完成判题后才会产生掌握度记录。可以先下发一次作业，或让学生做一次入学摸底。" />
        </CLCard>
        <div v-else class="heat-scroll">
          <table class="heat">
            <caption class="sr-only">
              {{ heat.course_display }}掌握度热力图：每行一名学生，每列一个知识点，格内数字为掌握度百分比
            </caption>
            <thead>
              <tr>
                <th class="sticky-col" scope="col">学生</th>
                <th class="num" scope="col" title="已练知识点的平均掌握度，与学生首页同一口径">平均</th>
                <!-- 列名横排、最多两行：原先竖排，中文逐字往下排、和英文混排时挤成一团，
                     长名还被 112px 的高度截断。全名、章节和练过人数在 title 里 -->
                <th v-for="p in heat.points" :key="p.knowledge_id" class="col-name" scope="col"
                  :title="`${p.chapter} · ${p.name}（${p.learners} 人练过，${p.error_open} 条未订正）`">
                  <span>{{ p.name }}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="s in heat.students" :key="s.username" :class="{ idle: s.avg_mastery === null }">
                <th class="sticky-col" scope="row">
                  <!-- 点名字看这个人。此前热力图能看出谁掉队，但点不进去 ——
                       发现问题之后没有任何动作可做 -->
                  <button class="s-open" @click="openStudent(s.username)">
                    <b>{{ s.name || s.username }}</b>
                    <small>{{ s.class_name || '未填班级' }} · {{ s.attempts }} 次作答</small>
                  </button>
                </th>
                <td class="num">{{ pct(s.avg_mastery) }}</td>
                <td v-for="p in heat.points" :key="p.knowledge_id">
                  <!-- 未评估画留白：给它一个"很低"的颜色，等于把没教到说成没学会 -->
                  <span v-if="s.cells[String(p.knowledge_id)]"
                    :class="['cell', 'lv-' + levelOf(s.cells[String(p.knowledge_id)].mastery).key]"
                    :style="cellStyle(s.cells[String(p.knowledge_id)].mastery)"
                    :title="cellLabel(s.name || s.username, p.name, s.cells[String(p.knowledge_id)].mastery)"
                    :aria-label="cellLabel(s.name || s.username, p.name, s.cells[String(p.knowledge_id)].mastery)"
                    role="img">{{ Math.round(s.cells[String(p.knowledge_id)].mastery * 100) }}</span>
                  <span v-else class="cell empty" role="img" :title="`${p.name}：未评估`"
                    :aria-label="`${s.name || s.username}，${p.name}，未评估`" />
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <CLCard title="本门共性错因" class="heat-errors">
          <CLEmpty v-if="!heat.common_errors.length" title="这门课还没有错题记录"
            hint="等班里的学生开始作答，这里会按错因聚合出共性问题，供你挑选作业知识点。" />
          <ul v-else class="ce-list">
            <li v-for="c in heat.common_errors" :key="c.error_type">
              <CLProgress :value="c.count / Math.max(1, heat.common_errors[0].count) * 100"
                :label="c.error_name" :right="String(c.count)" tone="warn" />
            </li>
          </ul>
        </CLCard>
      </template>
    </template>

    <!-- ============ 复核队列 ============ -->
    <template v-else-if="tab === 'queue'">
      <div class="scope-bar">
        <CLSegmented v-model="qReviewed" :options="QUEUE_FILTERS" />
        <select class="cl-input klass" v-model="qReason">
          <option v-for="o in REASON_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
        </select>
        <CLSegmented v-model="ovScope"
          :options="[{ label: courseStore.info?.short || '当前课程', value: 'course' },
                     { label: '全部课程', value: 'all' }]" />
        <select class="cl-input klass" v-model="ovClass">
          <option value="">全部班级</option>
          <option v-for="k in classes" :key="k" :value="k">{{ k }}</option>
        </select>
        <span v-if="queue" class="thin-note">
          共 {{ queue.total }} 条，当前第 {{ queue.offset + 1 }}–{{ Math.min(queue.offset + queue.limit, queue.total) }} 条
        </span>
      </div>

      <CLCard v-if="qaRequests.length" title="学生对 AI 问答的复核申请"
        :sub="`${qaTotal} 条在等回复 · 回复会出现在学生的「错题与学习记录」里`" class="qa-card">
        <ul class="rc-list">
          <li v-for="item in qaRequests" :key="item.id" class="rc-item">
            <div class="rc-top">
              <button class="s-open inline" @click="openStudent(item.username)">{{ item.username }}</button>
              <CLBadge v-if="item.no_evidence" variant="warn">AI 当时没找到教材依据</CLBadge>
              <span class="rc-at">{{ (item.created_at || '').slice(5, 16) }}</span>
            </div>
            <div class="qa-block"><span class="qa-label">学生的问题</span><MarkdownView :source="item.question || '（未记录问题）'" /></div>
            <!-- 直接展开：判断"AI 说得对不对"最需要看的就是这段，折起来助教还得多点一下 -->
            <div class="qa-block"><span class="qa-label">AI 的回答</span>
              <div class="qa-answer"><MarkdownView :source="item.ai_answer || '（未记录）'" /></div></div>
            <div v-if="replyFor === item.id" class="rc-edit">
              <CLSegmented v-model="replyVerdict"
                :options="[{ label: 'AI 说得对', value: 'agree' }, { label: '需要更正', value: 'corrected' }]" />
              <CLField v-model="replyText" textarea :rows="3"
                :placeholder="replyVerdict === 'corrected' ? '写明正确的说法（必填）' : '补充说明（可选）'" />
              <div class="rc-btns">
                <CLButton variant="primary" :disabled="submitting" @click="sendReply(item)">回复学生</CLButton>
                <CLButton variant="text" @click="replyFor = 0">取消</CLButton>
              </div>
            </div>
            <div v-else class="rc-btns rc-actions">
              <CLButton variant="ghost" @click="startReply(item.id)">回复</CLButton>
            </div>
          </li>
        </ul>
      </CLCard>

      <CLCard title="错题复核" sub="复核结果会用于改进后续的 AI 判定">
        <CLSkeleton v-if="queueLoading" :rows="5" />
        <CLEmpty v-else-if="!queue || !queue.items.length"
          :title="qReviewed === 'no' ? '没有待复核的错题' : '没有匹配的记录'"
          :hint="qReviewed === 'no' ? '这个范围内的错题都已经看过了。' : '换个筛选条件试试。'" />
        <template v-else>
          <ul class="rc-list">
            <li v-for="r in queue.items" :key="r.id" class="rc-item" :class="{ reviewed: r.ta_reviewed }">
              <div class="rc-top">
                <button class="s-open inline" @click="openStudent(r.username)">{{ r.username }}</button>
                <CLBadge v-if="r.ta_reviewed" variant="ok">
                  {{ r.ta_verdict === 'override' ? '已改判' : '已确认' }}
                </CLBadge>
                <span class="rc-at">{{ (r.created_at || '').slice(5, 16) }}</span>
              </div>
              <ReviewContext :r="r" />
              <p v-if="r.ta_note" class="rc-note">批注：{{ r.ta_note }}</p>
              <ul v-if="r.impact?.length" class="rc-impact">
                <li v-for="(x, k) in r.impact" :key="k">{{ x }}</li>
              </ul>
              <div v-if="openId === r.id" class="rc-edit">
                <CLSelect v-model="pickName" :groups="tagGroups" :allow-create="false"
                  placeholder="选择纠正后的错因" />
                <CLField v-model="note" textarea :rows="2" placeholder="给学生/给自己的备注（可选）" />
                <div class="rc-btns">
                  <CLButton variant="primary" :disabled="submitting" @click="submitOverride(r)">提交改判</CLButton>
                  <CLButton variant="text" @click="cancelOverride">取消</CLButton>
                </div>
              </div>
              <div v-else-if="!r.ta_reviewed" class="rc-btns rc-actions">
                <CLButton variant="ghost" :disabled="submitting" @click="confirmRecord(r)">确认判定</CLButton>
                <CLButton variant="text" :disabled="submitting" @click="startOverride(r.id)">改判</CLButton>
              </div>
            </li>
          </ul>
          <div class="pager">
            <CLButton variant="ghost" :disabled="queue.offset <= 0"
              @click="loadQueue(Math.max(0, queue.offset - queue.limit))">上一页</CLButton>
            <CLButton variant="ghost" :disabled="queue.offset + queue.limit >= queue.total"
              @click="loadQueue(queue.offset + queue.limit)">下一页</CLButton>
          </div>
        </template>
      </CLCard>
    </template>

    <!-- ============ 学生详情抽屉 ============ -->
    <!-- 抽屉而不是跳转新页面：助教是在"扫一遍全班"的动作里点开某个人，
         跳走再回来会丢掉刚才看到哪一行 -->
    <div v-if="detailFor" class="drawer-mask" @click.self="detailFor = ''">
      <aside class="drawer">
        <header class="dr-head">
          <div>
            <b>{{ detail?.profile.name || detailFor }}</b>
            <small v-if="detail">{{ detail.profile.student_id || '无学号' }} ·
              {{ detail.profile.class_name || '未填班级' }}</small>
          </div>
          <button class="dr-close" @click="detailFor = ''">关闭</button>
        </header>

        <CLSkeleton v-if="detailLoading" :rows="6" />
        <template v-else-if="detail">
          <div class="dr-stats">
            <CLStat :value="String(detail.activity.attempts || 0)" label="作答次数" />
            <CLStat :value="String(detail.activity.correct_n || 0)" label="其中做对" />
            <CLStat :value="detail.placement ? detail.placement.level_name : '未测'" label="入学摸底" />
            <CLStat :value="detail.review ? `${Math.round(detail.review.completion_rate * 100)}%` : '无计划'"
              label="复习计划完成" />
          </div>
          <p class="dr-when" v-if="detail.activity.last_at">
            最后作答 {{ detail.activity.last_at.slice(0, 16) }}
          </p>

          <!-- 与学生自己首页 / 记录页上的同一个数、同一句覆盖说明（I-03） -->
          <p class="dr-when">
            平均掌握度 {{ masteryPct(detail.mastery_summary?.average) }} · {{ coverageText(detail.mastery_summary) }}
          </p>

          <h4 class="dr-title">最薄弱的知识点</h4>
          <CLEmpty v-if="!detail.mastery.length" title="还没有掌握度数据"
            hint="他还没做过这门课的题，或做的题没有关联知识点。" />
          <ul v-else class="dr-mastery">
            <li v-for="m in detail.mastery.slice(0, 8)" :key="m.knowledge_id">
              <span>{{ m.knowledge_name }}</span>
              <CLProgress :value="m.mastery * 100" :right="pct(m.mastery)"
                :tone="m.mastery < 0.6 ? 'warn' : 'brand'" />
            </li>
          </ul>

          <h4 class="dr-title">最近错题</h4>
          <CLEmpty v-if="!detail.errors.length" title="没有错题记录" hint="这门课他还没有被记下的错题。" />
          <ul v-else class="dr-errors">
            <li v-for="e in detail.errors" :key="e.id">
              <div class="de-top">
                <CLBadge variant="warn">{{ e.error_name }}</CLBadge>
                <span class="de-kp">{{ e.knowledge || '未关联知识点' }}</span>
                <CLBadge v-if="e.ta_reviewed" variant="ok">已复核</CLBadge>
                <CLBadge v-else-if="e.resolved" variant="cite">已订正</CLBadge>
                <span class="de-at">{{ (e.created_at || '').slice(5, 10) }}</span>
              </div>
              <div class="de-diag"><MarkdownView :source="e.diagnosis || ''" /></div>
              <p v-if="e.ta_note" class="de-note">批注：{{ e.ta_note }}</p>
            </li>
          </ul>
        </template>
      </aside>
    </div>

    <!-- ============ 作业 ============ -->
    <template v-else-if="tab === 'assignment'">
      <CLCard title="下发作业" sub="选知识点 + 定目标题数；学生不需要「领取」，做对题就算完成">
        <div class="form">
          <CLField v-model="form.title" label="标题" placeholder="如：第三章 进程调度 巩固" />
          <CLField v-model="form.note" label="说明（可选）" textarea :rows="2" placeholder="给学生的一句话" />
          <div class="picker">
            <div class="picker-head">
              <span>知识点</span>
              <b>已选 {{ picked.length }} 个</b>
              <button v-if="picked.length" class="linkbtn" @click="picked = []">清空</button>
              <input v-if="points.length" v-model="pointQuery" class="cl-input picker-search" type="search"
                placeholder="搜索知识点或章节" aria-label="搜索知识点或章节" />
            </div>
            <CLEmpty v-if="!points.length" title="这门课还没有知识点"
              hint="这门课的知识点大纲尚未配置，请联系教务导入后再下发作业。" />
            <p v-else-if="!pointGroups.length" class="thin-note">没有匹配「{{ pointQuery }}」的知识点。</p>
            <div v-for="g in pointGroups" :key="g.chapter" class="chapter">
              <button class="chapter-name" @click="toggleChapter(g.items)">{{ g.chapter }}</button>
              <div class="chips">
                <button v-for="p in g.items" :key="p.id" type="button"
                  :class="['chip', { on: picked.includes(p.id) }]" @click="togglePoint(p.id)">
                  {{ p.name }}
                </button>
              </div>
            </div>
          </div>
          <div class="form-row">
            <label><span>每个知识点做对</span>
              <input class="cl-input tiny" type="number" min="1" max="20" v-model="form.target" />
            </label>
            <label><span>截止日期</span>
              <input class="cl-input" type="date" v-model="form.due" />
            </label>
            <label><span>班级</span>
              <select class="cl-input" v-model="form.klass">
                <option value="">全体</option>
                <option v-for="k in classes" :key="k" :value="k">{{ k }}</option>
              </select>
            </label>
            <label v-if="sourceOptions.length"><span>针对错因</span>
              <select class="cl-input" v-model="form.source">
                <option value="">不指定（手动挑的）</option>
                <option v-for="o in sourceOptions" :key="o.code" :value="o.code">{{ o.label }}</option>
              </select>
            </label>
          </div>
          <div class="publish-bar">
            <div class="publish-summary" aria-live="polite">
              <template v-if="picked.length">
                <b>{{ form.title.trim() || '（未填标题）' }}</b>
                <span>{{ publishSummary }}</span>
                <span class="publish-points">{{ pickedNames.join('、') }}</span>
              </template>
              <span v-else>还没有选知识点。</span>
            </div>
            <CLButton :disabled="publishing" @click="publish">{{ publishing ? '下发中…' : '确认下发' }}</CLButton>
          </div>
          <p class="thin-note">
            验收口径：下发之后、在这些知识点上做对的<b>不同题目</b>数量。
            同一道题反复提交只算一次；下发之前做过的不计入。
          </p>
        </div>
      </CLCard>

      <CLEmpty v-if="!assignments.length" title="还没有下发过作业"
        hint="选几个共性错因集中的知识点，定个目标题数即可" />
      <CLCard v-for="a in assignments" :key="a.id" class="asg">
        <div class="asg-head">
          <b>{{ a.title }}</b>
          <CLBadge v-if="a.status !== 'open'" variant="neutral">已关闭</CLBadge>
          <CLBadge v-else-if="a.overdue" variant="err">已逾期</CLBadge>
          <CLBadge v-else variant="ok">进行中</CLBadge>
          <CLBadge variant="cite">{{ a.class_name || '全体' }}</CLBadge>
          <span class="asg-meta">
            每点 {{ a.target_count }} 题
            <template v-if="a.due_date"> · 截止 {{ a.due_date }}</template>
            · 下发于 {{ a.created_at.slice(5, 16) }}
          </span>
          <CLButton variant="text" :disabled="!!exporting" @click="exportCsv('assignment', a.id)">导出名单</CLButton>
          <CLButton v-if="a.status === 'open'" variant="text" @click="close(a)">关闭</CLButton>
        </div>
        <p class="asg-source">
          <template v-if="a.source_name">
            来源：班级共性错因「{{ a.source_name }}」<template v-if="a.source_follow_up"> · 处置：{{ a.source_follow_up }}</template>
          </template>
          <template v-else>来源：助教手动创建</template>
        </p>
        <p v-if="a.note" class="asg-note">{{ a.note }}</p>
        <div class="asg-points">
          <CLBadge v-for="p in a.points" :key="p.knowledge_id">{{ p.name }}</CLBadge>
        </div>
        <CLProgress :value="a.completion_rate * 100"
          :label="`完成 ${a.completed_students}/${a.total_students} 人`"
          :right="`${Math.round(a.completion_rate * 100)}%`"
          :tone="a.completion_rate < 0.5 ? 'warn' : 'brand'" />
        <!-- 效果验证：只报真实记录算出的数，样本不够不下结论（docs/35 P1-7） -->
        <p v-if="a.effect" class="asg-effect">
          <template v-if="a.effect.pairs">
            目标知识点平均掌握度 {{ pctText(a.effect.before) }} → {{ pctText(a.effect.after) }}（{{ a.effect.pairs }} 组下发前有记录的学生×知识点）
          </template>
          <template v-else>下发前这些知识点没有掌握度记录，暂无前后对比</template>
          <template v-if="a.effect.attempts_after">
            · 下发后作答 {{ a.effect.attempts_after }} 次，正确率 {{ pctText(a.effect.accuracy_after) }}
          </template>
          <template v-if="!a.effect.enough"> · 参与学生不足 5 人，先不下结论</template>
        </p>
        <button class="asg-toggle" @click="openAssignment = openAssignment === a.id ? null : a.id">
          {{ openAssignment === a.id ? '收起名单' : '按人查看' }}
        </button>
        <ul v-if="openAssignment === a.id" class="asg-list">
          <li v-for="s in a.students" :key="s.username" :class="{ done: s.completed }">
            <span class="s-name">{{ s.name || s.username }}</span>
            <span class="s-num">{{ s.points_done }}/{{ s.points_total }} 点</span>
            <CLBadge :variant="s.completed ? 'ok' : 'warn'">{{ s.completed ? '已完成' : '未完成' }}</CLBadge>
          </li>
        </ul>
      </CLCard>
    </template>

  </div>
</template>

<style scoped>
.wrap { max-width: 1180px; margin: 0 auto; }
.thin-note { font-size: 12.5px; line-height: 1.7; color: var(--cl-text-3); }
.qa-card { margin-bottom: 16px; }
.today-focus.clcard { margin-bottom: 16px; background: var(--cl-accent-surface); border-color: var(--cl-tint-border); }
.today-focus.calm.clcard { background: var(--cl-row); }
.today-focus .eyebrow { font-size: 12px; font-weight: 800; letter-spacing: .08em; color: var(--cl-accent-soft); }
.today-focus h2 { margin: 6px 0 4px; font-size: 20px; }
.today-focus p { margin: 0 0 12px; color: var(--cl-text-2); }
.today-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; align-items: start; }
@media (max-width: 980px) { .today-grid { grid-template-columns: 1fr; } }
.today-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.row-link {
  width: 100%; display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 8px 10px; border: 1px solid transparent; border-radius: var(--cl-radius-sm);
  background: transparent; color: inherit; font: inherit; font-size: 13px; text-align: left; cursor: pointer;
}
.row-link:hover { background: var(--cl-row-hover); border-color: var(--cl-row-border-hover); }
.row-link small { color: var(--cl-text-3); margin-left: 4px; }
.row-link b { font-family: var(--cl-mono); }
.row-link b.hot { color: var(--cl-err-fg, var(--cl-warn-fg)); }
.asg-effect { margin: 8px 0 0; font-size: 12.5px; color: var(--cl-text-3); line-height: 1.7; }
.rc-impact { margin: 6px 0 0; padding-left: 18px; font-size: 12.5px; color: var(--cl-text-3); line-height: 1.7; }
.qa-block { margin: 8px 0; font-size: 13.5px; color: var(--cl-text-2); }
.qa-label { display: block; font-size: 12px; font-weight: 700; color: var(--cl-text-3); margin-bottom: 4px; cursor: default; }
.qa-answer {
  max-height: 220px; overflow: auto; padding: 8px 10px; border-radius: var(--cl-radius-sm);
  background: var(--cl-row); border: 1px solid var(--cl-row-border);
}
.tiles { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 16px; }
/* 上下排、各占满宽：左右并排时错因卡只有几行、错题卡很长，左下空一大块，错题也被挤得很窄 */
.cols { display: flex; flex-direction: column; gap: 18px; }
/* 错因卡占满宽后单列进度条会拉得过长，宽屏下排成两列 */
.ce-list { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px 32px; }

/* ---------- 热力图 ---------- */
.heat-bar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 14px; }
.klass { width: 150px; }
.legend { display: flex; align-items: center; gap: 4px; margin-left: auto; font-size: 11.5px; color: var(--cl-text-3); }
.legend i { width: 16px; height: 12px; border-radius: 3px; display: inline-block; }
.legend b { font-weight: 600; margin: 0 8px 0 4px; }
.legend .empty, .cell.empty {
  background: repeating-linear-gradient(45deg, var(--cl-hatch) 0 3px, transparent 3px 6px);
  box-shadow: 0 0 0 1px var(--cl-row-border) inset;
}
/* 横向滚动容器：知识点最多 40 列，铺不下是常态。表头与首列都吸附，
   滚到中间时还能知道自己在看谁的哪个点。 */
.heat-scroll {
  overflow: auto; max-height: 68vh; margin-bottom: 18px;
  border: 1px solid var(--cl-glass-border); border-radius: var(--cl-radius-sm);
  background: var(--cl-inset);
}
/* 作用域条：筛选与说明并排，四个数是按它算的 */
.scope-bar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 4px; }
.pager { display: flex; justify-content: center; gap: 10px; margin-top: 14px; }
.rc-at { margin-left: auto; font-size: 11.5px; color: var(--cl-text-3); }
.rc-note {
  margin: 6px 0 0; padding-left: 8px; font-size: 12.5px; line-height: 1.6;
  border-left: 3px solid var(--cl-cite-bd, var(--cl-border)); color: var(--cl-text);
}
.s-open.inline { display: inline; width: auto; font-weight: 700; color: var(--cl-text); }
.s-open.inline:hover { text-decoration: underline; }

/* ---- 学生详情抽屉 ---- */
.drawer-mask {
  position: fixed; inset: 0; z-index: 40;
  background: rgba(0, 0, 0, .38); display: flex; justify-content: flex-end;
}
.drawer {
  width: min(460px, 92vw); height: 100%; overflow-y: auto; padding: 18px 20px 28px;
  background: var(--cl-surface); border-left: 1px solid var(--cl-border);
  display: flex; flex-direction: column; gap: 14px;
}
.dr-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.dr-head b { display: block; font-size: 16px; color: var(--cl-text); }
.dr-head small { display: block; margin-top: 3px; font-size: 12px; color: var(--cl-text-3); }
.dr-close {
  font: inherit; font-size: 12px; padding: 4px 10px; cursor: pointer;
  border-radius: var(--cl-radius-sm); border: 1px solid var(--cl-border);
  background: transparent; color: var(--cl-text-2);
}
.dr-stats { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
.dr-when { margin: -6px 0 0; font-size: 12px; color: var(--cl-text-3); }
.dr-title { margin: 6px 0 0; font-size: 13px; font-weight: 700; color: var(--cl-text); }
.dr-mastery { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 9px; }
.dr-mastery li { display: flex; flex-direction: column; gap: 4px; font-size: 12.5px; color: var(--cl-text-2); }
.dr-errors { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }
.dr-errors li {
  padding: 9px 11px; border: 1px solid var(--cl-row-border); border-radius: var(--cl-radius-sm);
}
.de-top { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.de-kp { font-size: 12px; color: var(--cl-text-2); font-weight: 600; }
.de-at { margin-left: auto; font-size: 11.5px; color: var(--cl-text-3); }
.de-diag { margin: 6px 0 0; font-size: 12.5px; color: var(--cl-text-2); line-height: 1.6; }
/* 批注单独一条竖线：它是人写的，与 AI 诊断要分得开 */
.de-note {
  margin: 6px 0 0; padding-left: 8px; font-size: 12.5px; line-height: 1.6;
  border-left: 3px solid var(--cl-cite-bd, var(--cl-border)); color: var(--cl-text);
}
/* 学生名做成按钮但保持原来的排版：整格可点，命中面积才够 */
.s-open {
  display: block; width: 100%; text-align: left; cursor: pointer;
  background: transparent; border: 0; padding: 0; font: inherit; color: inherit;
}
.s-open:hover b { text-decoration: underline; }

/* 导出按钮推到工具条最右：它不是筛选，是"看完之后带走"，位置上要与筛选分开 */
.exports { margin-left: auto; display: flex; gap: 8px; }

.heat { border-collapse: separate; border-spacing: 3px; font-size: 12px; }
.heat thead th {
  position: sticky; top: 0; z-index: 2; padding: 6px 4px;
  background: var(--cl-popover);
  color: var(--cl-text-3); font-weight: 600; text-align: left;
}
/* 列名竖排：40 列横排会把表宽拉到几千像素 */
/* 固定高度而不是 max-height：竖排文字的"长度"是它的高度，表头单元格按内容收缩时，
   只给上限不给定值，竖排 span 的高度会被算成 0 —— 列名整排消失（视觉走查发现，
   Playwright 量到 20 个列名全是 0 尺寸）。 */
.heat th.col-name { vertical-align: bottom; width: 52px; min-width: 52px; max-width: 52px; cursor: default; }
.heat th.col-name span {
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  font-size: 11px; line-height: 1.3; overflow-wrap: anywhere; text-align: center;
}
.heat .sticky-col {
  position: sticky; left: 0; z-index: 3; min-width: 132px; text-align: left;
  background: var(--cl-popover); padding: 6px 10px;
}
.heat thead .sticky-col { z-index: 4; }
.heat tbody .sticky-col b { display: block; font-size: 12.5px; color: var(--cl-text); font-weight: 600; }
.heat tbody .sticky-col small { display: block; margin-top: 2px; font-size: 11px; color: var(--cl-text-3); }
.heat th.num { white-space: nowrap; }
.legend-levels { color: var(--cl-text-2); }
.heat td.num, .heat th.num {
  text-align: right; padding-right: 8px; font-variant-numeric: tabular-nums; color: var(--cl-text-2);
}
.cell {
  position: relative; display: flex; align-items: center; justify-content: center;
  width: 52px; height: 28px; border-radius: 4px;
  font-size: 11.5px; font-weight: 700; font-variant-numeric: tabular-nums;
}
/* 薄弱格的角标：颜色之外的第二个信号，扫一眼就能找出需要盯的格子 */
.cell.lv-weak::after, .legend .weak-mark::after {
  content: ''; position: absolute; top: 0; right: 0;
  border-style: solid; border-width: 0 8px 8px 0;
  border-color: transparent var(--cl-warn-fg) transparent transparent;
  border-top-right-radius: 4px;
}
.legend .weak-mark { position: relative; background: var(--cl-row); box-shadow: 0 0 0 1px var(--cl-row-border) inset; }
.sr-only {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
}
/* 一行全是留白 = 这个学生还没开始。整行压暗，让"掉队的"和"没开始的"分得开 */
.heat tr.idle .sticky-col b { color: var(--cl-text-3); }

.heat-errors { margin-bottom: 18px; }

/* ---------- 作业 ---------- */
.form { display: flex; flex-direction: column; gap: 12px; }
.form-row { display: flex; align-items: flex-end; gap: 14px; flex-wrap: wrap; }
.form-row label { display: flex; flex-direction: column; gap: 6px; font-size: 12.5px; font-weight: 600; color: var(--cl-text-2); }
.cl-input.tiny { width: 88px; }
/* 知识点芯片墙：压暗一档并单独描边，读起来是"表单里的一块选择区"。
   章节名可点（整章全选/全不选）—— 下发作业多半就是按章来的。 */
.picker {
  padding: 12px; border-radius: var(--cl-radius-sm);
  background: var(--cl-inset); border: 1px solid var(--cl-glass-border);
  max-height: 34vh; overflow: auto;
}
.picker-head {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: -12px -12px 10px; padding: 12px 12px 8px;
  font-size: 12.5px; position: sticky; top: -12px; z-index: 1; background: var(--cl-inset);
}
.picker-search { margin-left: auto; width: 200px; max-width: 100%; height: 32px; font-size: 12.5px; }
.publish-bar {
  display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
  padding: 10px 12px; border-radius: var(--cl-radius-sm);
  border: 1px solid var(--cl-tint-border); background: var(--cl-tint);
}
.publish-summary { flex: 1 1 280px; min-width: 0; display: flex; flex-direction: column; gap: 2px; font-size: 12.5px; color: var(--cl-text-2); }
.publish-summary b { font-size: 13.5px; color: var(--cl-text); }
.publish-points { color: var(--cl-text-3); overflow-wrap: anywhere; }
.picker-head span { font-weight: 600; color: var(--cl-text-2); }
.picker-head b { color: var(--cl-accent-soft); }
.linkbtn { border: 0; background: none; padding: 0; color: var(--cl-brand-600); font: inherit; font-size: 12.5px; cursor: pointer; }
.chapter { margin-bottom: 10px; }
.chapter-name {
  display: block; margin-bottom: 6px; border: 0; background: none; padding: 0;
  font: inherit; font-size: 11.5px; font-weight: 700; letter-spacing: .04em;
  color: var(--cl-text-3); cursor: pointer;
}
.chapter-name:hover { color: var(--cl-brand-600); }
.chips { display: flex; gap: 6px; flex-wrap: wrap; }
.chip {
  padding: 5px 10px; border-radius: var(--cl-radius-pill); cursor: pointer;
  border: 1px solid var(--cl-row-border); background: var(--cl-row);
  color: var(--cl-text-2); font: inherit; font-size: 12px;
  transition: background .12s ease, border-color .12s ease, color .12s ease;
}
.chip:hover { background: var(--cl-row-hover); border-color: var(--cl-row-border-hover); color: var(--cl-text); }
.chip.on { background: var(--cl-brand-grad); border-color: transparent; color: var(--cl-on-brand); box-shadow: var(--cl-glow); }
.chip:focus-visible { outline: 2px solid var(--cl-brand); outline-offset: 2px; }
.asg { margin-top: 16px; display: flex; flex-direction: column; gap: 10px; }
.asg-source { font-size: 12.5px; color: var(--cl-text-3); margin: 6px 0 0; }
.asg-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.asg-head b { font-size: 15px; }
.asg-meta { font-size: 12px; color: var(--cl-text-3); margin-left: auto; }
.asg-note { margin: 0; font-size: 13px; color: var(--cl-text-2); }
.asg-points { display: flex; gap: 6px; flex-wrap: wrap; }
.asg-toggle {
  align-self: flex-start; border: 0; background: none; padding: 0;
  color: var(--cl-brand-600); font: inherit; font-size: 12.5px; cursor: pointer;
}
.asg-toggle:hover { color: var(--cl-brand-700); }
.asg-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.asg-list li {
  display: flex; align-items: center; gap: 10px; padding: 8px 11px;
  border-radius: var(--cl-radius-sm); background: var(--cl-row); border: 1px solid var(--cl-row-border);
  font-size: 13px;
}
.asg-list li.done { opacity: .7; }
.s-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.s-num { font-variant-numeric: tabular-nums; color: var(--cl-text-3); }

/* ---------- 复核（原有） ---------- */
.rc-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 12px; }
.rc-item {
  padding: 11px 13px 11px 15px; border-radius: var(--cl-radius-sm);
  background: var(--cl-row); border: 1px solid var(--cl-row-border);
  border-left: 3px solid var(--cl-brand);
  box-shadow: var(--cl-edge-glow);
  transition: background .15s ease, border-color .15s ease, box-shadow .15s ease;
}
.rc-item:hover { background: var(--cl-row-hover); }
.rc-item.reviewed { border-left-color: var(--cl-border-strong); box-shadow: none; opacity: .84; }
.rc-item.reviewed .rc-user, .rc-item.reviewed .rc-was { color: var(--cl-text-2); }
.rc-item.reviewed:hover { opacity: 1; }
.rc-top { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.rc-user { font-size: 12px; color: var(--cl-text-3); margin-left: auto; font-family: var(--cl-mono); }
.rc-was { font-size: 11.5px; color: var(--cl-text-3); }
.rc-diag { font-size: 13px; color: var(--cl-text-2); margin-top: 6px; line-height: 1.6; }
.rc-reviewed { display: flex; align-items: center; gap: 8px; margin-top: 9px; flex-wrap: wrap; }
.rc-note { font-size: 12.5px; color: var(--cl-text-3); }
.rc-override {
  display: flex; flex-direction: column; gap: 8px; margin-top: 10px;
  padding: 11px; border-radius: var(--cl-radius-sm);
  background: var(--cl-inset); border: 1px solid var(--cl-glass-border);
}
.rc-btns { display: flex; gap: 8px; flex-wrap: wrap; }
.rc-actions { margin-top: 10px; }

@media (max-width: 820px) {
  .tiles { grid-template-columns: repeat(2, 1fr); }
  .legend { margin-left: 0; }
}
/* 作业表单在窄屏改单列（I-08）：题量 / 截止日期 / 班级 / 错因各占一行、输入框占满，
   下发按钮也占满整行，不再挤在一排里互相压缩 */
@media (max-width: 620px) {
  .form-row label { flex: 1 1 100%; }
  .form-row .cl-input, .form-row .cl-input.tiny { width: 100%; }
  .picker-search { margin-left: 0; width: 100%; }
  .publish-bar button { width: 100%; }
}
/* 手机宽度：冻结的学生列只留姓名，把屏宽让给知识点列（其余横向滚动） */
@media (max-width: 620px) {
  .heat .sticky-col { min-width: 84px; max-width: 96px; padding: 6px 8px; }
  .heat tbody .sticky-col small { display: none; }
  .heat tbody .sticky-col b { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
}
</style>
