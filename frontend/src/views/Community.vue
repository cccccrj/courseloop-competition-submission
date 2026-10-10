<script setup lang="ts">
/**
 * 学习态势与社区：连续学习天数、本周匿名榜、同伴对比、最近两周的作答折线。
 *
 * 这页最容易做坏的地方是**让落后的人不想打开它**。三条渲染纪律：
 *
 * 1) **榜是匿名的**。后端除自己外根本不下发身份字段（backend/learning/community.py），
 *    这里也只显示"第 N 名"。同伴对比要给的是"我在什么位置"，不是"谁比我强"。
 * 2) **"这周没练"不等于"垫底"**。没上榜时不画一行灰色的自己排最后，
 *    而是单独说一句"这周还没有记录"，并给一个去练习的入口。
 * 3) **对比给中位数，不给第一名**。跟榜首比只会劝退；中位数回答的是
 *    "我和大多数人差多少"，那才是可行动的信息。
 */
import { computed, onActivated, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { get } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useCourseStore } from '@/stores/course'
import CLBadge from '@/components/ui/CLBadge.vue'
import CLButton from '@/components/ui/CLButton.vue'
import CLCard from '@/components/ui/CLCard.vue'
import CLEmpty from '@/components/ui/CLEmpty.vue'
import CLPageHeader from '@/components/ui/CLPageHeader.vue'
import CLProgress from '@/components/ui/CLProgress.vue'
import CLSkeleton from '@/components/ui/CLSkeleton.vue'
import CLStat from '@/components/ui/CLStat.vue'

interface BoardRow {
  rank: number; is_me: boolean; alias: string
  attempts: number; correct_n: number; active_days: number
  correct_rate: number; avg_mastery: number | null
}
interface Standings {
  course_display: string; week_start: string; week_end: string; offset: number
  active_students: number
  board: BoardRow[]
  me: {
    on_board: boolean; rank: number | null; percentile: number | null
    attempts: number; correct_n: number; active_days: number
    correct_rate: number | null; avg_mastery: number | null
    points_covered: number; streak_days: number
  }
  peers: { median_correct: number | null; median_mastery: number | null; learners: number }
  trend: { day: string; attempts: number; correct_n: number }[]
}

const router = useRouter()
const auth = useAuthStore()
const courseStore = useCourseStore()

const data = ref<Standings | null>(null)
const loading = ref(true)
const failed = ref(false)
const offset = ref(0)
const MAX_OFFSET = 52

const weekLabel = computed(() =>
  offset.value === 0 ? '本周' : offset.value === 1 ? '上周' : `${offset.value} 周前`)

const trendMax = computed(() =>
  Math.max(1, ...(data.value?.trend || []).map((d) => d.attempts)))

// 与同伴中位数的差。null 表示没有可比对象，页面上要说"暂无可比"而不是 0。
const correctGap = computed(() => {
  const m = data.value?.peers.median_correct
  if (m === null || m === undefined || !data.value) return null
  return data.value.me.correct_n - m
})
const masteryGap = computed(() => {
  const m = data.value?.peers.median_mastery
  const mine = data.value?.me.avg_mastery
  if (m === null || m === undefined || mine === null || mine === undefined) return null
  return mine - m
})

async function load() {
  loading.value = true
  failed.value = false
  try {
    const q = new URLSearchParams({ course: courseStore.current, offset: String(offset.value) })
    data.value = await get<Standings>('/community/standings?' + q.toString())
  } catch {
    // 原先没有这个 catch：请求一挂 data 停在 null，模板里 v-else-if="data" 整段不渲染，
    // 页面上只剩一个标题 —— 那就是真正的"白屏"，而且没有任何下一步。
    failed.value = true
    data.value = null
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

function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : `${Math.round(v * 100)}%`
}
function signed(v: number | null, digits = 0): string {
  if (v === null) return '—'
  const s = digits ? v.toFixed(digits) : String(Math.round(v))
  return v > 0 ? `+${s}` : s
}

onActivated(async () => { await courseStore.load(); await load() })
watch(() => courseStore.current, () => { offset.value = 0; load() })
</script>

<template>
  <div class="community">
    <CLPageHeader title="学习态势" sub="连续学习天数和本周匿名榜，榜上除你之外只显示名次">
      <template #actions>
        <div class="weeknav">
          <button class="wbtn" :disabled="offset >= MAX_OFFSET" title="更早一周" @click="step(1)">‹</button>
          <span class="wlabel">{{ weekLabel }}</span>
          <button class="wbtn" :disabled="offset <= 0" title="更近一周" @click="step(-1)">›</button>
        </div>
      </template>
    </CLPageHeader>

    <CLSkeleton v-if="loading" :rows="6" />
    <CLEmpty v-else-if="failed" title="学习态势没能加载出来"
      hint="可能是网络或服务暂时不可用，稍等一下再试。">
      <CLButton @click="load">重试</CLButton>
    </CLEmpty>
    <template v-else-if="data">
      <div class="range">
        <CLBadge variant="cite">{{ data.course_display }}</CLBadge>
        <span>{{ data.week_start }} 至 {{ data.week_end }} · 本周 {{ data.active_students }} 人在练</span>
      </div>

      <div class="stats">
        <CLStat :value="`${data.me.streak_days} 天`" label="连续学习" accent />
        <CLStat :value="data.me.correct_n" label="本周做对" />
        <CLStat :value="data.me.rank === null ? '—' : `第 ${data.me.rank} 名`" label="本周名次" />
        <CLStat :value="pct(data.me.percentile)" label="超过同学" />
      </div>

      <div class="grid">
        <CLCard title="本周榜" sub="按本周做对的题数排名">
          <CLEmpty v-if="!data.board.length" :title="`${weekLabel}还没有人练习`"
            hint="第一个开始的人就是第一名" />
          <ul v-else class="board">
            <li v-for="r in data.board" :key="r.rank + (r.is_me ? '-me' : '')"
              :class="{ me: r.is_me, top: r.rank <= 3 }">
              <span class="b-rank">{{ r.rank }}</span>
              <span class="b-alias">{{ r.alias }}</span>
              <span class="b-num">{{ r.correct_n }} 题</span>
              <span class="b-sub">{{ pct(r.correct_rate) }} · {{ r.active_days }} 天</span>
            </li>
          </ul>
          <!-- 没上榜时单独说一句，而不是画一行"我排最后" -->
          <div v-if="data.board.length && !data.me.on_board" class="notyet">
            <b>{{ weekLabel }}你还没有学习记录</b>
            <p>榜上只有本周练过的人。做一道题就会出现在这里。</p>
            <CLButton variant="ghost" @click="router.push({ name: 'practice' })">去练几道</CLButton>
          </div>
        </CLCard>

        <CLCard title="和同学比" sub="和班级中位数比">
          <CLEmpty v-if="!data.peers.learners" title="还没有同学的数据可比"
            hint="等这门课有更多人开始学习后，这里会给出中位数对比" />
          <template v-else>
            <div class="cmp">
              <div class="cmp-row">
                <span class="c-label">本周做对</span>
                <b class="c-mine">{{ data.me.correct_n }}</b>
                <span class="c-vs">中位 {{ data.peers.median_correct }}</span>
                <CLBadge :variant="(correctGap || 0) >= 0 ? 'ok' : 'warn'">
                  {{ signed(correctGap) }} 题
                </CLBadge>
              </div>
              <div class="cmp-row">
                <span class="c-label">平均掌握度</span>
                <b class="c-mine">{{ pct(data.me.avg_mastery) }}</b>
                <span class="c-vs">中位 {{ pct(data.peers.median_mastery) }}</span>
                <CLBadge :variant="(masteryGap || 0) >= 0 ? 'ok' : 'warn'">
                  {{ masteryGap === null ? '—' : signed(masteryGap * 100) + '%' }}
                </CLBadge>
              </div>
            </div>
            <p class="thin-note">
              掌握度只统计你练过的 {{ data.me.points_covered }} 个知识点，没练过的不计入。
            </p>
          </template>
        </CLCard>

        <CLCard title="最近两周" sub="每天的作答量，深色部分是做对的">
          <div class="trend">
            <div v-for="d in data.trend" :key="d.day" class="t-col" :title="`${d.day}：${d.correct_n}/${d.attempts} 对`">
              <span class="t-bar" :style="{ height: `${(d.attempts / trendMax) * 100}%` }">
                <i :style="{ height: `${d.attempts ? (d.correct_n / d.attempts) * 100 : 0}%` }" />
              </span>
              <small>{{ d.day.slice(8) }}</small>
            </div>
          </div>
          <p class="thin-note">
            已连续 {{ data.me.streak_days }} 天有学习记录。今天还没练不会清零，从昨天算起。
          </p>
        </CLCard>

        <CLCard title="分享" sub="把这周的成绩或某一道错题做成图片">
          <div class="share">
            <CLButton variant="ghost" @click="router.push({ name: 'report' })">导出本周学情卡</CLButton>
            <CLButton variant="ghost" @click="router.push({ name: 'records' })">分享一道错题</CLButton>
          </div>
          <p class="thin-note">
            错题卡不带正确答案，方便和同学讨论。
          </p>
        </CLCard>
      </div>
    </template>
  </div>
</template>

<style scoped>
.community { max-width: 1120px; margin: 0 auto; }
.thin-note { margin: 12px 0 0; font-size: 12.5px; line-height: 1.7; color: var(--cl-text-3); }

.weeknav {
  display: flex; align-items: center; gap: 2px;
  background: var(--cl-row); border: 1px solid var(--cl-row-border);
  border-radius: var(--cl-radius-pill); padding: 3px;
}
.wbtn {
  width: 28px; height: 28px; border: 0; border-radius: 50%;
  background: transparent; color: var(--cl-text-2); cursor: pointer; font-size: 17px; line-height: 1;
}
.wbtn:hover:not(:disabled) { background: var(--cl-row-hover); color: var(--cl-text); }
.wbtn:disabled { opacity: .32; cursor: default; }
.wlabel { font-size: 13px; font-weight: 700; color: var(--cl-text); min-width: 52px; text-align: center; }

.range { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; font-size: 13px; color: var(--cl-text-3); }
.stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 16px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; align-items: start; }

/* ---------- 榜 ---------- */
.board { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.board li {
  display: grid; grid-template-columns: 30px 1fr auto auto; align-items: center; gap: 10px;
  padding: 9px 11px; border-radius: var(--cl-radius-sm);
  background: var(--cl-row); border: 1px solid var(--cl-row-border);
}
/* 自己那一行是整张榜唯一需要一眼找到的东西 */
.board li.me {
  background: var(--cl-tint-grad);
  border-color: var(--cl-tint-border-strong); box-shadow: var(--cl-glow);
}
.b-rank { font-family: var(--cl-mono); font-size: 13px; color: var(--cl-text-3); text-align: center; }
.board li.top .b-rank { color: var(--cl-accent-soft); font-weight: 700; }
.b-alias { font-size: 13.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.board li.me .b-alias { font-weight: 700; }
.b-num { font-size: 13.5px; font-weight: 700; font-variant-numeric: tabular-nums; }
.b-sub { font-size: 12px; color: var(--cl-text-3); font-variant-numeric: tabular-nums; }

.notyet {
  margin-top: 12px; padding: 12px 14px; border-radius: var(--cl-radius-sm);
  border-left: 3px solid var(--cl-warn); background: var(--cl-inset);
}
.notyet b { font-size: 14px; }
.notyet p { margin: 6px 0 10px; font-size: 12.5px; color: var(--cl-text-3); line-height: 1.7; }

/* ---------- 同伴对比 ---------- */
.cmp { display: flex; flex-direction: column; gap: 10px; }
.cmp-row {
  display: grid; grid-template-columns: 84px auto 1fr auto; align-items: baseline; gap: 10px;
  padding: 11px 13px; border-radius: var(--cl-radius-sm);
  background: var(--cl-row); border: 1px solid var(--cl-row-border);
}
.c-label { font-size: 12.5px; color: var(--cl-text-3); }
.c-mine { font-size: 22px; font-variant-numeric: tabular-nums; }
.c-vs { font-size: 12.5px; color: var(--cl-text-3); }

/* ---------- 折线（柱） ---------- */
/* 柱状而不是折线：14 个点的折线在窄卡里读不出量级差别，柱子能。
   每根柱里深色的一段是做对的部分 —— 高度是"做了多少"，实心是"对了多少"。 */
.trend { display: flex; align-items: flex-end; gap: 5px; height: 132px; }
.t-col { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 5px; height: 100%; }
.t-bar {
  position: relative; width: 100%; min-height: 3px; margin-top: auto;
  border-radius: 4px 4px 2px 2px; background: var(--cl-tint-strong);
  display: flex; align-items: flex-end;
}
.t-bar i {
  display: block; width: 100%; border-radius: inherit;
  background: linear-gradient(180deg, var(--cl-accent), var(--cl-brand));
  box-shadow: var(--cl-glow);
}
.t-col small { font-size: 10.5px; color: var(--cl-text-3); font-family: var(--cl-mono); }

.share { display: flex; gap: 10px; flex-wrap: wrap; }

@media (max-width: 820px) { .stats, .grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 620px) { .stats, .grid { grid-template-columns: 1fr; } }
</style>
