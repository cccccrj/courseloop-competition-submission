<script setup lang="ts">
import { computed, ref, onActivated, watch } from 'vue'
import { useRouter } from 'vue-router'
import { get, post } from '@/api/client'
import { toast } from '@/api/toast'
import { useAuthStore } from '@/stores/auth'
import { useCourseStore } from '@/stores/course'
import { drawErrorCard, downloadDataUrl } from '@/utils/shareCard'
import CLPageHeader from '@/components/ui/CLPageHeader.vue'
import CLBadge from '@/components/ui/CLBadge.vue'
import CLButton from '@/components/ui/CLButton.vue'
import CLCard from '@/components/ui/CLCard.vue'
import CLProgress from '@/components/ui/CLProgress.vue'
import CLSkeleton from '@/components/ui/CLSkeleton.vue'
import CLEmpty from '@/components/ui/CLEmpty.vue'
import CLStat from '@/components/ui/CLStat.vue'
import MarkdownView from '@/components/MarkdownView.vue'
import { coverageText, masteryPct, type MasterySummary } from '@/utils/mastery'

const courseStore = useCourseStore()
const auth = useAuthStore()
const router = useRouter()
const errors = ref<any[]>([])
const mastery = ref<any[]>([])
const requests = ref<any[]>([])
const loading = ref(true)

const stateLabel: Record<string, string> = {
  not_started: '未开始', learning: '学习中', fragile: '不稳固',
  mastered: '已掌握', needs_review: '待复习', misconception: '概念错误',
}
const toneOf = (m: any) => (m.state === 'misconception' ? 'err' : m.mastery >= 0.8 ? 'brand' : 'warn')

// 计数用后端给的 counts：列表最多 50 条，拿 length 算会在错题多时和首页对不上
const errorCounts = ref<{ total: number; open: number; resolved: number } | null>(null)
const resolvedCount = computed(() => errorCounts.value?.resolved ?? errors.value.filter((e) => e.resolved).length)
const openCount = computed(() => errorCounts.value?.open ?? errors.value.length - resolvedCount.value)
const errorTotal = computed(() => errorCounts.value?.total ?? errors.value.length)
// 平均掌握度不在前端自己求：与学习首页同一口径，由后端 summary 给出（I-03）
const masterySummary = ref<MasterySummary | null>(null)

async function load() {
  loading.value = true
  try {
    const c = encodeURIComponent(courseStore.current)
    const e: any = await get('/records/errors?course=' + c); errors.value = e.items || []
    errorCounts.value = e.counts || null
    const m: any = await get('/records/mastery?course=' + c); mastery.value = m.items || []
    masterySummary.value = m.summary || null
    const q: any = await get('/review-requests?course=' + c).catch(() => ({ items: [] }))
    requests.value = q.items || []
  } finally { loading.value = false }
}

// onActivated 而非 onMounted：页面被 keep-alive 缓存后 onMounted 只跑一次，
// 去做几道题回来就还是旧数据。onActivated 首次挂载也会触发，一处即可。
onActivated(async () => { await courseStore.load(); await load() })
watch(() => courseStore.current, load)
const openStem = ref(new Set<number>())
function toggleStem(id: number) {
  const next = new Set(openStem.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  openStem.value = next
}

// 请助教看看这条诊断（docs/35 P1-2）。只标记，不改判 —— 判不判错是助教的事
const requesting = ref(0)
async function requestReview(e: any) {
  if (requesting.value) return
  requesting.value = e.id
  try {
    const r: any = await post('/review-requests', { error_record_id: e.id })
    Object.assign(e, { review_requested: true, review_requested_at: r.requested_at, ta_reviewed: false })
    toast('已提交给助教，处理后会显示在这条错题下面')
  } finally {
    requesting.value = 0
  }
}

function retry(e: any) {
  router.push({ path: '/practice', query: { course: courseStore.current, kid: e.knowledge_id } })
}

// 错题分享卡。诊断里存的是 "题干 || 诊断" 拼起来的串（db.save_error_record），
// 卡上要分开显示，所以按同一个分隔符拆回去。
function shareError(e: any) {
  try {
    const url = drawErrorCard({
      courseDisplay: courseStore.info?.display || '',
      who: auth.displayName,
      knowledge: e.knowledge_name || e.knowledge || '未归类知识点',
      errorName: e.error_name || '',
      stem: e.stem || (e.diagnosis || '').split(' || ')[0] || '',
      diagnosis: (e.diagnosis || '').split(' || ').slice(-1)[0] || '',
      createdAt: (e.created_at || '').slice(0, 10),
      resolved: !!e.resolved,
    })
    downloadDataUrl(url, `CourseLoop-错题-${(e.error_name || 'card')}-${e.id}.png`)
  } catch (err) {
    console.error('[share]', err)
    toast('导出图片没有成功，请重试', 'error')
  }
}
</script>

<template>
  <div class="wrap">
    <CLPageHeader title="错题与学习记录" sub="你的错题、助教的复核结论和各知识点掌握度" />

    <div v-if="!loading" class="stats">
      <CLStat :value="errorTotal" label="错题总数" accent />
      <CLStat :value="openCount" label="待解决" />
      <CLStat :value="resolvedCount" label="已解决" />
      <CLStat :value="masteryPct(masterySummary?.average)"
        :label="masterySummary ? `平均掌握度 · ${coverageText(masterySummary)}` : '平均掌握度'" />
    </div>

    <div class="cols">
      <CLCard>
        <template #header><div class="sec-head"><span class="sec-title">错题本</span><CLBadge>{{ errors.length }} 条</CLBadge></div></template>
        <CLSkeleton v-if="loading" :card="false" :rows="4" />
        <CLEmpty v-else-if="!errors.length" title="还没有错题" hint="去「解题纠错」练几道吧" />
        <ul v-else class="err-list">
          <li v-for="e in errors" :key="e.id" class="err-item" :class="{ resolved: e.resolved }">
            <div class="err-top">
              <CLBadge variant="warn">{{ e.error_name }}</CLBadge>
              <CLBadge :variant="e.resolved ? 'ok' : 'err'">{{ e.resolved ? '已解决' : '待解决' }}</CLBadge>
              <span class="err-date">{{ e.created_at }}</span>
            </div>
            <!-- 题干现在存的是全文（此前只有前 80 字），长题会把卡片撑满半屏。
                 默认收成三行，点一下展开 -->
            <div v-if="e.stem" class="err-stem" :class="{ clamp: !openStem.has(e.id) }"
              :title="openStem.has(e.id) ? '' : '点击展开题目全文'" @click="toggleStem(e.id)">
              <MarkdownView :source="e.stem" />
            </div>
            <div class="err-diag"><MarkdownView :source="e.diagnosis || ''" /></div>
            <!-- 助教的人工复核。此前它写在库里但接口不往外给，学生永远看不到，
                 而"有人真的看过这道题"恰恰是这条错题最值钱的部分。
                 放在 AI 诊断下面而不是替换它：两者是"机器判的"和"老师确认过的"，
                 学生要能分得出哪句话是谁说的。 -->
            <div v-if="e.ta_reviewed" class="err-ta">
              <span class="ta-tag">{{ e.ta_verdict === 'override' ? '老师订正了错因' : '老师已复核' }}</span>
              <span v-if="e.ta_note" class="ta-note">{{ e.ta_note }}</span>
              <!-- 改判却没写批注时，原先也显示"老师确认了这条判定"，和上面的"订正了错因"自相矛盾 -->
              <span v-else-if="e.ta_verdict === 'override'" class="ta-note muted">
                老师把错因改成了「{{ e.error_name }}」，上面的 AI 诊断仅供参考。</span>
              <span v-else class="ta-note muted">老师确认了这条判定，没有补充说明。</span>
            </div>
            <div v-else-if="e.review_requested" class="err-ta pending">
              <span class="ta-tag">已请助教复核</span>
              <span class="ta-note muted">提交于 {{ (e.review_requested_at || '').slice(5, 16) }}，助教处理后结论会显示在这里。</span>
            </div>
            <div class="err-action">
              <CLButton v-if="!e.ta_reviewed && !e.review_requested" variant="text"
                :disabled="requesting === e.id" @click="requestReview(e)">觉得判得不对？请助教看看</CLButton>
              <CLButton variant="text" @click="shareError(e)">分享这道题</CLButton>
              <CLButton v-if="e.knowledge_id" variant="ghost" @click="retry(e)">去重练</CLButton>
            </div>
          </li>
        </ul>
      </CLCard>

      <CLCard v-if="requests.length">
        <template #header><div class="sec-head"><span class="sec-title">我提交的问答复核</span><CLBadge>{{ requests.length }} 条</CLBadge></div></template>
        <ul class="err-list">
          <li v-for="q in requests" :key="q.id" class="err-item">
            <div class="err-top">
              <CLBadge :variant="q.status === 'answered' ? 'ok' : 'warn'">{{ q.status === 'answered' ? '助教已回复' : '等待助教' }}</CLBadge>
              <CLBadge v-if="q.no_evidence" variant="neutral">当时没有教材依据</CLBadge>
              <span class="err-date">{{ (q.created_at || '').slice(5, 16) }}</span>
            </div>
            <div class="err-stem"><MarkdownView :source="q.question || ''" /></div>
            <div v-if="q.status === 'answered'" class="err-ta">
              <span class="ta-tag">{{ q.ta_verdict === 'corrected' ? '助教给了更正' : '助教确认 AI 说得对' }}</span>
              <span v-if="q.ta_reply" class="ta-note"><MarkdownView :source="q.ta_reply" /></span>
            </div>
          </li>
        </ul>
      </CLCard>

      <CLCard>
        <template #header><div class="sec-head"><span class="sec-title">知识点掌握度</span><CLBadge>{{ mastery.length }} 项</CLBadge></div></template>
        <CLSkeleton v-if="loading" :card="false" :rows="4" />
        <CLEmpty v-else-if="!mastery.length" title="这门课还没有掌握度数据"
          hint="先做几道题，这里会列出每个知识点的掌握度。">
          <CLButton @click="router.push({ name: 'practice' })">去做几道练习</CLButton>
        </CLEmpty>
        <ul v-else class="mastery-list">
          <li v-for="m in mastery" :key="m.knowledge_id">
            <CLProgress
              :value="(m.mastery || 0) * 100"
              :label="m.knowledge_name || ('知识点 ' + m.knowledge_id)"
              :right="stateLabel[m.state] || '—'"
              :tone="toneOf(m)"
            />
          </li>
        </ul>
      </CLCard>
    </div>
  </div>
</template>

<style scoped>
.wrap { max-width: 1000px; margin: 0 auto; }
.stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 18px; }
.cols { display: grid; grid-template-columns: 1.2fr 1fr; gap: 18px; align-items: start; }
.sec-head { display: flex; justify-content: space-between; align-items: center; width: 100%; }
.sec-title { font-weight: 700; letter-spacing: .2px; }

/* 老师那一段用左边一道竖线与 AI 诊断分开：颜色用引用色而不是警告色 ——
   它是一条"有人看过"的凭据，不是又一个提醒 */
.err-ta {
  margin-top: 8px; padding: 8px 10px; border-left: 3px solid var(--cl-cite-bd, var(--cl-border));
  background: var(--cl-row-hover); border-radius: 0 var(--cl-radius-sm) var(--cl-radius-sm) 0;
  display: flex; flex-direction: column; gap: 4px;
}
.ta-tag { font-size: 11.5px; font-weight: 700; color: var(--cl-text-3); }
.ta-note { font-size: 13px; color: var(--cl-text); line-height: 1.6; }
.ta-note.muted { color: var(--cl-text-3); }

.err-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 12px; }
/* 错题条：左侧强调线是这一列的主要信息层次 —— 一眼扫下去哪些还没解决。
   线本身发光而不只是变色，深色下"亮"比"红"更快被看到。
   条目底走 --cl-row（卡片里的行不再叠玻璃），不用 --cl-surface-2。 */
.err-item {
  position: relative; overflow: hidden;
  padding: 12px 14px 12px 16px; border-radius: var(--cl-radius-sm);
  background: var(--cl-row); border: 1px solid var(--cl-row-border);
  border-left: 3px solid var(--cl-warn);
  box-shadow: var(--cl-edge-glow-warn);
  transition: background .15s ease, border-color .15s ease, box-shadow .15s ease;
}
.err-item:hover { background: var(--cl-row-hover); }
/* 已解决的条目降一档：仍然可查，但不该继续占据注意力。
   用绿色而不是直接调灰 —— "解决了"是个成果，不是"作废了"。 */
.err-item.resolved {
  border-left-color: var(--cl-ok);
  box-shadow: var(--cl-edge-glow-ok);
  opacity: .82;
}
/* opacity 作用于整条，条里最弱的一级文字会跟着一起降 —— .err-date 用的是
   --cl-text-3，乘上 .82 后只剩 3.9:1，掉到 AA 线下。把已解决条目里的这一级
   提到 --cl-text-2，降档后仍有 6.3:1，而整条"退后一步"的观感不变。 */
.err-item.resolved .err-date { color: var(--cl-text-2); }
.err-item.resolved:hover { opacity: 1; }
.err-top { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.err-date { font-size: 12px; color: var(--cl-text-3); font-family: var(--cl-mono); margin-left: auto; }
.err-stem { font-weight: 600; margin: 8px 0 4px; cursor: pointer; }
.err-stem.clamp { display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.err-diag { font-size: 13.5px; color: var(--cl-text-2); line-height: 1.6; }
.err-action { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; }

.mastery-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 16px; }
@media (max-width: 820px) { .stats { grid-template-columns: 1fr 1fr; } .cols { grid-template-columns: 1fr; } }
@media (max-width: 560px) { .stats { grid-template-columns: 1fr; } }
</style>
