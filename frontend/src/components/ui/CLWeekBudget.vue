<script setup lang="ts">
// 备考时间预算编辑器：周模板 + 例外日 + 强度 + 休息日。
//
// 为什么是「每天填一个数」而不是「每周填一个总数」：工作日满课、周末整天空是
// 考研备考最常见的形态，一个周总数摊到七天必然摊错 —— 它不知道你周三没空。
// 也不做「几点到几点」的时段表：刷题这类任务不需要精确到时刻，
// 而时段表要处理时区、跨天和重叠，填写成本和实现成本都翻倍，收益却只有好看。
//
// 预算是**全局**的（一个人一天的总时间），不属于某一门课；界面底部实时显示
// 它按 408 分值分给当前这门课多少，让「为什么我这门课每天只排了 20 分钟」有答案。
import { computed, nextTick, ref, watch } from 'vue'
import CLButton from './CLButton.vue'
import CLSegmented from './CLSegmented.vue'

export interface BudgetException { date: string; minutes: number; note: string }
export interface Budget {
  weekly_minutes: number[]
  intensity: string
  buffer_weekday: number
  exam_date: string
  exceptions: BudgetException[]
  course_shares?: Record<string, number>
  is_default?: boolean
}

const props = defineProps<{
  modelValue: Budget; course: string; busy?: boolean
  /** 从复习页周网格点进来时带的星期几（0=周一..6=周日，-1 表示没指定）。
      高亮并聚焦那一格 —— 点了"周三"就该直接能改周三，而不是自己再找一遍。 */
  focusDay?: number
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: Budget): void; (e: 'save'): void }>()

const WEEKDAYS = ['一', '二', '三', '四', '五', '六', '日']
const INTENSITY = [
  { label: '轻', value: 'light' },
  { label: '适中', value: 'medium' },
  { label: '冲刺', value: 'sprint' },
]
// 每个档位对应一套推荐排法：七天分钟数（周一…周日）+ 休息日。休息日一起给，
// 是因为"周日休息"本身就是这套排法的一部分——只给时间不给休息日的话，选完档位
// 还得自己再去点一次"设为休息"，而那一步很容易忘，忘了就是七天连排。
//
// ⚠️ medium 这一档必须与后端 study_budget.DEFAULT_WEEKLY 逐项相同。
// 后端新用户的默认就是 (intensity=medium, DEFAULT_WEEKLY)；两边对不上的话，
// 一个什么都没动的人打开页面会看到"适中"配着一组不属于该档的数字，
// 下面那句"你已手动调整过"还会平白诬告他一次。改这一档时同步改后端那个常量。
// tests/test_frontend_constants.py 会挡住单边修改。
const PRESETS: Record<string, { minutes: number[]; buffer: number }> = {
  // 一三五 4 小时、二四六 3 小时，周日休息 —— 21 小时/周
  light: { minutes: [240, 180, 240, 180, 240, 180, 0], buffer: 6 },
  // 周一到周六各 8 小时，周日休息 —— 48 小时/周（= DEFAULT_WEEKLY）
  medium: { minutes: [480, 480, 480, 480, 480, 480, 0], buffer: 6 },
  // 每天 16 小时，没有休息日 —— 112 小时/周
  sprint: { minutes: [960, 960, 960, 960, 960, 960, 960], buffer: -1 },
}
// 与后端 study_budget.FILL_RATIO 一致。这里只用于显示「系统只排 ~70%」这句话，
// 真正的排期折扣在服务端算 —— 两边对不上只会让文案不准，不会让计划算错。
const FILL: Record<string, number> = { light: 0.6, medium: 0.72, sprint: 0.85 }

// 界面按小时填，接口仍然收发分钟：weekly_minutes / exceptions[].minutes 是后端
// study_budget 与整条排期链路的单位，换存储单位要动服务端、迁移旧数据和一批用例，
// 而这里要解决的只是"填 90 还是填 1.5 更像人话"。所以只在读写的那一层换算。
const MAX_HOURS = 16
const MAX_MINUTES = MAX_HOURS * 60
const GRID_HOURS = [4, 8, 12]

/** 分钟 → 小时，去掉无意义的小数尾巴（90 → 1.5，60 → 1）。 */
function toHours(minutes: number): number {
  return Math.round(((Number(minutes) || 0) / 60) * 100) / 100
}
function fmtHours(minutes: number): string {
  return String(toHours(minutes))
}

const local = ref<Budget>(clone(props.modelValue))
watch(() => props.modelValue, (v) => {
  local.value = clone(v)
  // 外部数据换了（保存后回填、切换用户），挂起的确认就失去了前提：
  // 它描述的是"你填的那份时间"，而那份已经不在了。
  pendingIntensity.value = ''
}, { deep: true })

function clone(b: Budget): Budget {
  return { ...b, weekly_minutes: [...(b.weekly_minutes || [])],
           exceptions: (b.exceptions || []).map((e) => ({ ...e })) }
}

// 柱子的高度基准是**固定的 16 小时**，不是这七天里的最大值。
// 用最大值当基准时，"每天 1 小时"和"每天 8 小时"画出来一模一样——柱子只表达了
// 一周的形状，完全没表达投入量。改成绝对刻度后，填满才是满，一眼看得出还剩多少天。
// 代价是常见取值（1~3 小时）只占槽高的一小截，所以槽加高到 120px 并画了
// 4/8/12 小时三条刻度线；没有刻度的绝对轴等于让人对着一根短棍猜数。
// 注意这与 Review.vue 那排只读柱子的基准**不再相同**（那边仍按周内最大值），
// 两者回答的问题不一样：这里是"我给出多少"，那边是"这一周的形状和已被占掉的部分"。
function fillPct(i: number) {
  return Math.round(((local.value.weekly_minutes?.[i] || 0) / MAX_MINUTES) * 100)
}
// 留白刻度线的位置：系统实际只排到 分钟 × 填充率 这条线。把它画在用户**正在填数字**
// 的那一刻，而不是等保存完到另一个页面才看得见。
function capPct(i: number) {
  const ratio = FILL[local.value.intensity] ?? 0.72
  return Math.round(fillPct(i) * ratio)
}
const dayInputs = ref<HTMLInputElement[]>([])
watch(() => props.focusDay, async (i) => {
  if (i === undefined || i < 0) return
  await nextTick()
  const el = dayInputs.value[i]
  if (!el || el.disabled) return
  el.focus()
  el.select()
}, { immediate: true })

function push() { emit('update:modelValue', clone(local.value)) }

/** 选档位 = 换一套推荐时间。七天全覆盖，不做"保留我填过的那几天"的部分合并——
    部分覆盖的结果没法预期（选了冲刺却发现周三还是 0），不如一次覆盖干净再微调。 */
function applyIntensity(value: string) {
  local.value.intensity = value
  const preset = PRESETS[value]
  if (preset) {
    local.value.weekly_minutes = [...preset.minutes]
    local.value.buffer_weekday = preset.buffer
  }
  pendingIntensity.value = ''
  push()
}

// 待确认的档位切换。**只在覆盖会真的丢东西时才拦**：当前七天还等于本档推荐值时，
// 覆盖是无损的，再弹一次确认纯属噪音，而每次都要确认的提示很快就会被无脑点掉。
const pendingIntensity = ref('')
const pendingLabel = computed(() =>
  INTENSITY.find((o) => o.value === pendingIntensity.value)?.label || '')
const pendingTotalHours = computed(() =>
  fmtHours((PRESETS[pendingIntensity.value]?.minutes || []).reduce((a, b) => a + b, 0)))

function pickIntensity(value: string) {
  if (value === local.value.intensity) return
  if (isPreset.value) applyIntensity(value)   // 没动过，无损，直接换
  else pendingIntensity.value = value         // 有自定义，等确认；此刻不改任何数据
}

/** 只换档位、保留用户自己填的七天。
    档位同时是"系统排多满"的折扣系数（FILL），把它和推荐时间绑死之后，
    想单独调折扣就没有入口了 —— 这一项就是那个入口，不是可有可无的第三个按钮。 */
function switchIntensityKeepTime() {
  local.value.intensity = pendingIntensity.value
  pendingIntensity.value = ''
  push()
}
function cancelIntensity() { pendingIntensity.value = '' }

const presetOf = computed(() => PRESETS[local.value.intensity] || PRESETS.medium)
const presetTotalHours = computed(() => fmtHours(presetOf.value.minutes.reduce((a, b) => a + b, 0)))
// 档位说明整句都从 PRESETS 推出来，不另写一份文案。三档的形状已经各不相同
// （轻是单双日交替、适中是周一到周六、冲刺是七天全满），任何"工作日 X / 周末 Y"
// 之类的模板句都描述不了它们，而写死的句子迟早和数组对不上。
const presetRestLabel = computed(() =>
  presetOf.value.buffer < 0 ? '无休息日' : `周${WEEKDAYS[presetOf.value.buffer]}休息`)
// 当前七天（含休息日）是否还等于该档位的推荐值。不等就说明用户自己动过，得说出来——
// 否则界面显示"冲刺"，而下面的数字其实是他改过的，读起来像是档位没生效。
const isPreset = computed(() => {
  const now = local.value.weekly_minutes || []
  return presetOf.value.minutes.every((m, i) => m === now[i])
    && presetOf.value.buffer === local.value.buffer_weekday
})

function setDay(i: number, value: string) {
  // 入参是小时，落库是分钟。上限 16 小时：再多就不是"每天可用时间"而是笔误了
  // （原来的上限是 720 分钟＝12 小时，这次按 16 小时放宽）。
  const hours = Math.max(0, Math.min(MAX_HOURS, Number(value) || 0))
  local.value.weekly_minutes[i] = Math.round(hours * 60)
  push()
}
function setException(e: BudgetException, value: string) {
  const hours = Math.max(0, Math.min(MAX_HOURS, Number(value) || 0))
  e.minutes = Math.round(hours * 60)
  push()
}
function toggleBuffer(i: number) {
  local.value.buffer_weekday = local.value.buffer_weekday === i ? -1 : i
  push()
}
function addException() {
  const d = new Date(); d.setDate(d.getDate() + 1)
  const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  local.value.exceptions.push({ date: iso, minutes: 0, note: '' })
  push()
}
function removeException(i: number) { local.value.exceptions.splice(i, 1); push() }

const weeklyTotal = computed(() =>
  (local.value.weekly_minutes || []).reduce((a, b) => a + (Number(b) || 0), 0))
const share = computed(() => props.modelValue.course_shares?.[props.course] ?? 1)
const ratio = computed(() => FILL[local.value.intensity] ?? 0.72)
const courseWeekly = computed(() => Math.round(weeklyTotal.value * share.value * ratio.value))
const weeklyTotalHours = computed(() => fmtHours(weeklyTotal.value))
const courseWeeklyHours = computed(() => fmtHours(courseWeekly.value))
</script>

<template>
  <div class="budget">
    <div class="row">
      <span class="row-label">每天可用时间（小时，最多 {{ MAX_HOURS }}）</span>
      <div class="days">
        <label v-for="(d, i) in WEEKDAYS" :key="i"
               :class="['day', { buffer: local.buffer_weekday === i, off: !local.weekly_minutes[i],
                                 focus: props.focusDay === i }]">
          <span class="day-name">{{ d }}</span>
          <!-- 柱子跟着输入实时长高，编辑态因此和复习页那排只读柱子说同一种话。
               虚线是"系统实际只排到这里"，把留白提前到填数字的这一刻就看得见。 -->
          <span class="day-gauge" aria-hidden="true">
            <span v-if="local.buffer_weekday === i" class="day-rest">
              <svg viewBox="0 0 24 24">
                <path d="M20 14.5A8 8 0 0 1 9.5 4a8.2 8.2 0 1 0 10.5 10.5z" />
              </svg>
            </span>
            <template v-else>
              <!-- 绝对刻度的参照线：没有它，一根占槽高 12% 的短棍读不出是 2 小时还是 3 小时 -->
              <span v-for="h in GRID_HOURS" :key="h" class="day-grid"
                    :style="{ bottom: (h / MAX_HOURS) * 100 + '%' }"></span>
              <span class="day-fill" :style="{ height: fillPct(i) + '%' }"></span>
              <span v-if="local.weekly_minutes[i]" class="day-cap" :style="{ bottom: capPct(i) + '%' }"></span>
            </template>
          </span>
          <input class="cl-input day-input" type="number" min="0" :max="MAX_HOURS" step="0.5"
                 :ref="(el) => { if (el) dayInputs[i] = el as HTMLInputElement }"
                 :disabled="local.buffer_weekday === i"
                 :value="local.buffer_weekday === i ? '' : fmtHours(local.weekly_minutes[i])"
                 @input="setDay(i, ($event.target as HTMLInputElement).value)" />
          <button type="button" class="day-buffer" :title="local.buffer_weekday === i ? '取消休息日' : '设为休息日'"
                  @click="toggleBuffer(i)">{{ local.buffer_weekday === i ? '休息日' : '设为休息' }}</button>
        </label>
      </div>
    </div>

    <div class="row two">
      <!-- 这里包的是一组 button（CLSegmented），不是可关联的表单控件，
           用 <label> 只会让内部按钮的点击语义变可疑，改回 <div>。 -->
      <div class="ctl">
        <span>推荐档位</span>
        <CLSegmented :model-value="local.intensity" :options="INTENSITY"
                     @update:modelValue="pickIntensity" />
        <span class="ctl-hint">
          共 {{ presetTotalHours }} 小时每周 · {{ presetRestLabel }}；
          系统只排 ~{{ Math.round(ratio * 100) }}%。
          <template v-if="isPreset">换档位会覆盖下面七天的数字和休息日。</template>
          <b v-else>下面的数字你已手动调整过，与本档位推荐值不同。</b>
        </span>

        <!-- 覆盖前的确认。不用原生 confirm()：三档主题下它是系统弹窗，
             和整页材质完全不搭，而且拦不住"手比脑子快"——原生弹窗的默认焦点
             常常就在确定上。做成行内条还能顺便把新旧两个数字摆出来给人对比。 -->
        <div v-if="pendingIntensity" class="ctl-confirm">
          <span>
            换成「{{ pendingLabel }}」会用推荐值覆盖你填的七天和休息日：
            现在 <b>{{ weeklyTotalHours }}</b> 小时/周 → <b>{{ pendingTotalHours }}</b> 小时/周。
          </span>
          <div class="ctl-confirm-acts">
            <button type="button" class="cc-primary" @click="applyIntensity(pendingIntensity)">
              覆盖并换档
            </button>
            <button type="button" class="cc-ghost" @click="switchIntensityKeepTime">
              只换档位，保留我的时间
            </button>
            <button type="button" class="cc-text" @click="cancelIntensity">取消</button>
          </div>
        </div>
      </div>
      <label class="ctl">
        <span>初试日期</span>
        <input class="cl-input" type="date" v-model="local.exam_date" @change="push" />
      </label>
    </div>

    <div class="row">
      <div class="row-head">
        <span class="row-label">例外日期</span>
        <CLButton variant="ghost" @click="addException">添加</CLButton>
      </div>
      <p v-if="!local.exceptions.length" class="hint">
        实习、回家、期末周这类整段没时间的日子填在这里，它会盖过上面的周模板。
      </p>
      <div v-for="(e, i) in local.exceptions" :key="i" class="exception">
        <input class="cl-input" type="date" v-model="e.date" @change="push" />
        <input class="cl-input mins" type="number" min="0" :max="MAX_HOURS" step="0.5"
               :value="fmtHours(e.minutes)"
               @input="setException(e, ($event.target as HTMLInputElement).value)" />
        <span class="unit">小时</span>
        <input class="cl-input note" v-model="e.note" placeholder="备注（如 期末周）" @change="push" />
        <button type="button" class="drop" @click="removeException(i)" aria-label="删除">✕</button>
      </div>
    </div>

    <div class="foot">
      <div class="foot-copy">
        本周预算 <b>{{ weeklyTotalHours }}</b> 小时 ·
        本门约占 <b>{{ Math.round(share * 100) }}%</b> ·
        系统只排 <b>~{{ Math.round(ratio * 100) }}%</b>，约 <b>{{ courseWeeklyHours }}</b> 小时
        <!-- 明说"只排 70%"而不是偷偷打折：留白是这个功能的卖点，
             不讲出来会被当成"它少排了"。 -->
      </div>
      <CLButton :disabled="busy" @click="emit('save')">{{ busy ? '保存中…' : '保存并重排' }}</CLButton>
    </div>
  </div>
</template>

<style scoped>
.budget { display: flex; flex-direction: column; gap: 16px; }
.row { display: flex; flex-direction: column; gap: 9px; }
.row.two { flex-direction: row; gap: 18px; flex-wrap: wrap; }
.row-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.row-label { font-size: 12.5px; font-weight: 600; color: var(--cl-text-2); }
.ctl { display: flex; flex-direction: column; gap: 7px; font-size: 12.5px; font-weight: 600; color: var(--cl-text-2); }
/* 档位说明：常规字重的小字，不跟标签抢——它是解释，不是可点的东西 */
.ctl-hint { max-width: 340px; font-size: 11.5px; font-weight: 400; line-height: 1.55; color: var(--cl-text-3); }
.ctl-hint b { color: var(--cl-text-2); font-weight: 600; }

/* 覆盖确认条。用 tint 底 + 强描边，与休息日格子同一族语言（全站的"这里要你注意"），
   但不加辉光——它是一次性询问，不是持续状态。 */
.ctl-confirm {
  display: flex; flex-direction: column; gap: 9px;
  max-width: 340px; padding: 10px 12px; margin-top: 2px;
  border-radius: var(--cl-radius-sm);
  background: var(--cl-tint); border: 1px solid var(--cl-tint-border-strong);
  font-size: 11.5px; font-weight: 400; line-height: 1.6; color: var(--cl-text-2);
}
.ctl-confirm b { color: var(--cl-text); font-weight: 700; }
.ctl-confirm-acts { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.ctl-confirm button {
  border: 1px solid transparent; border-radius: var(--cl-radius-sm);
  padding: 6px 12px; cursor: pointer; font: inherit; font-size: 11.5px; font-weight: 700;
  transition: background .14s ease, border-color .14s ease, color .14s ease, filter .14s ease;
}
.cc-primary { background: var(--cl-brand-grad); color: var(--cl-on-brand); }
.cc-primary:hover { filter: brightness(1.08); }
.cc-ghost { background: transparent; border-color: var(--cl-glass-border); color: var(--cl-text-2); }
.cc-ghost:hover { border-color: var(--cl-tint-border-strong); color: var(--cl-text); }
.cc-text { background: transparent; color: var(--cl-text-3); padding-left: 4px; padding-right: 4px; }
.cc-text:hover { color: var(--cl-text-2); }
.hint { margin: 0; font-size: 12.5px; color: var(--cl-text-3); }

/* ---------- 周模板 ---------- */
.days { display: grid; grid-template-columns: repeat(7, 1fr); gap: 8px; }
/* 每一格是一个凹槽，与 CLField 的输入框同一种"陷进去"的处理 —— 这一排是在
   填数字，不是在读信息，材质上要和别处的表单一致。 */
.day {
  display: flex; flex-direction: column; align-items: center; gap: 6px;
  padding: 9px 6px 7px; border-radius: var(--cl-radius-sm);
  background: var(--cl-inset); border: 1px solid var(--cl-glass-border);
  transition: border-color .15s ease, box-shadow .15s ease, opacity .15s ease;
}
/* 填 0 的那天整格降一档：它不是"待填"，是用户明确说了没空，视觉上该退后。 */
.day.off { opacity: .62; }
/* 休息日用左边那条竖光条的同族语言（全站的状态标记）—— 深色下靠光比靠色管用 */
.day.buffer {
  border-color: var(--cl-tint-border-strong);
  box-shadow: var(--cl-edge-glow-ok, none), inset 0 0 0 1px var(--cl-tint);
}
/* 从周网格点进来的那一格：描边加光。不是持久状态，只为了让人一眼找到
   "我刚才点的是这天"，避免展开后还要自己数第几个格子。 */
.day.focus {
  border-color: var(--cl-brand); box-shadow: var(--cl-glow);
}
.day-name { font-size: 12px; font-weight: 700; color: var(--cl-text-2); }

/* 编辑态的柱子。材质与 Review.vue 的 .bar-track/.bar-fill 同源（同一组令牌、
   同一个渐变），让"我在这里填的数"和"复习页看到的那排柱子"认得出是一回事。 */
.day-gauge {
  position: relative; width: 100%; height: 120px;
  border-radius: 6px; overflow: hidden;
  background: var(--cl-inset-deep);
  display: flex; align-items: flex-end;
}
.day-fill {
  width: 100%; background: var(--cl-brand-grad);
  transition: height .18s ease;
}
/* 4/8/12 小时的参照线。比留白虚线更淡：它是背景刻度，不该抢"系统排到哪"的读数。 */
.day-grid {
  position: absolute; left: 0; right: 0; height: 0;
  border-top: 1px solid var(--cl-row-border);
  opacity: .55; pointer-events: none;
}
/* 虚线：系统实际只排到这条线以下，上面那截是留白 */
.day-cap {
  position: absolute; left: 0; right: 0; height: 0;
  border-top: 1px dashed var(--cl-on-brand);
  opacity: .72; transition: bottom .18s ease;
}
.day-rest {
  width: 100%; height: 100%;
  display: flex; align-items: center; justify-content: center;
  background: var(--cl-tint);
}
.day-rest svg {
  width: 18px; height: 18px; fill: none;
  stroke: var(--cl-brand-600); stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round;
}

.day-input { width: 100%; text-align: center; padding: 6px 4px; }
/* 休息日不填分钟数：留着一个可输入的框会让人以为"休息日也能排一点" */
.day-input:disabled { opacity: .45; cursor: not-allowed; }
.day-buffer {
  border: none; background: transparent; cursor: pointer; padding: 0;
  font: inherit; font-size: 10.5px; color: var(--cl-text-3);
  transition: color .15s ease;
}
.day-buffer:hover { color: var(--cl-brand); }
.day.buffer .day-buffer { color: var(--cl-accent-soft); font-weight: 700; }

/* ---------- 例外日 ---------- */
.exception { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.exception .mins { width: 88px; }
.exception .note { flex: 1; min-width: 140px; }
.unit { font-size: 12.5px; color: var(--cl-text-3); }
.drop {
  border: none; background: transparent; cursor: pointer; font: inherit;
  color: var(--cl-text-3); padding: 6px 8px; border-radius: var(--cl-radius-sm);
}
.drop:hover { color: var(--cl-err); background: var(--cl-tint); }

.foot {
  display: flex; align-items: center; justify-content: space-between; gap: 14px; flex-wrap: wrap;
  padding-top: 12px; border-top: 1px solid var(--cl-row-border);
}
.foot-copy { font-size: 13px; color: var(--cl-text-2); }
.foot-copy b { color: var(--cl-text); }

@media (max-width: 720px) {
  .days { grid-template-columns: repeat(4, 1fr); }
  .exception .note { min-width: 100%; }
}
</style>
