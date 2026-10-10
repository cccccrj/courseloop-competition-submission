<script setup lang="ts">
/**
 * 拍照批改：草稿纸照片 → 视觉转写 → 错因诊断 → 学生确认后收进错题本。
 *
 * 两处刻意的设计：
 * 1) 转写文本单独成卡且先于诊断出现 —— 学生要能当场看出"机器读错了"，
 *    否则他会拿着一份基于误读的诊断去改本来做对的步骤。
 * 2) 入库要点一下按钮，不自动写 —— 转写错一处会顺着
 *    错题本 → 薄弱画像 → 复习计划整条闭环脏下去。
 */
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { get, post } from '@/api/client'
import { postSSE, sseErrorText } from '@/api/sse'
import { toast } from '@/api/toast'
import { useCourseStore } from '@/stores/course'
import CLPageHeader from '@/components/ui/CLPageHeader.vue'
import CLButton from '@/components/ui/CLButton.vue'
import CLBadge from '@/components/ui/CLBadge.vue'
import CLField from '@/components/ui/CLField.vue'
import CLSelect from '@/components/ui/CLSelect.vue'
import CLThinkBlock from '@/components/ui/CLThinkBlock.vue'
import MarkdownView from '@/components/MarkdownView.vue'

const courseStore = useCourseStore()
const router = useRouter()
courseStore.load()

/* ---------- 失败出口（docs/35 P0-5） ----------
 * 此前任何失败都只弹一条 toast，学生手里只剩「换一张」。现在按原因给下一步：
 *   not_configured 功能没开通 → 手动输入，去智能解题
 *   unreadable     认不出字   → 重新识别 / 换一张 / 手动输入
 *   timeout        超时       → 重试（图还在）
 *   其他                      → 重试，带错误编号
 */
interface Failure { kind: string; message: string }
const failure = ref<Failure | null>(null)

/** 带着题目和转写稿去智能解题。转写稿是学生的解法，到那边进作答框。 */
function goSolve(withTranscript = true) {
  router.push({ name: 'solve', query: {
    problem: problem.value.trim() || undefined,
    answer: withTranscript && transcript.value ? transcript.value : undefined,
  } })
}

const visionReady = ref(true)
const errorTags = ref<{ code: string; name: string }[]>([])

onMounted(async () => {
  // 能力探测放在挂载时：postSSE 遇到非 2xx 只抛得出 "HTTP 503"，
  // 等学生拍完照选完图才告诉他"没配模型"太晚了。
  try {
    const h = await get<{ vision_ready?: boolean }>('/health')
    visionReady.value = !!h.vision_ready
  } catch { /* 探测失败不拦路，让真正的请求去报错 */ }
  try {
    const r = await get<{ error_tags: { code: string; name: string }[] }>('/error-tags')
    errorTags.value = r.error_tags || []
  } catch { /* 码表拿不到时下拉退化为可自由输入 */ }
})

/* ---------- 选图与压缩 ---------- */
const MAX_EDGE = 1600          // 手写笔迹要留得住，比头像的 320 方块大得多
const fileInput = ref<HTMLInputElement | null>(null)
const imageUrl = ref('')       // data URL，同时用作预览和上传载荷
const dragging = ref(false)

function imageFrom(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image()
    image.onload = () => resolve(image)
    image.onerror = reject
    image.src = url
  })
}

async function acceptFile(file: File | undefined | null) {
  if (!file) return
  if (!file.type.startsWith('image/') || file.size > 12 * 1024 * 1024) {
    toast('请选择 12MB 以内的图片', 'error')
    return
  }
  const source = URL.createObjectURL(file)
  try {
    const image = await imageFrom(source)
    // 按最长边等比缩放（不裁剪）：草稿纸是长方形，裁成方块会切掉解题步骤
    const scale = Math.min(1, MAX_EDGE / Math.max(image.naturalWidth, image.naturalHeight))
    const w = Math.round(image.naturalWidth * scale)
    const h = Math.round(image.naturalHeight * scale)
    const canvas = document.createElement('canvas')
    canvas.width = w; canvas.height = h
    canvas.getContext('2d')?.drawImage(image, 0, 0, w, h)
    imageUrl.value = canvas.toDataURL('image/jpeg', 0.85)
    reset()
  } catch {
    toast('图片读取失败，请换一张试试', 'error')
  } finally {
    URL.revokeObjectURL(source)
    if (fileInput.value) fileInput.value.value = ''
  }
}

function onPick(e: Event) { acceptFile((e.target as HTMLInputElement).files?.[0]) }
function onDrop(e: DragEvent) { dragging.value = false; acceptFile(e.dataTransfer?.files?.[0]) }
function onPaste(e: ClipboardEvent) {
  const item = Array.from(e.clipboardData?.items || []).find((i) => i.type.startsWith('image/'))
  if (item) acceptFile(item.getAsFile())
}
onMounted(() => window.addEventListener('paste', onPaste))
onUnmounted(() => { window.removeEventListener('paste', onPaste); abort?.() })

/* ---------- 批改 ---------- */
const problem = ref('')
const busy = ref(false)
const stage = ref<'' | 'transcribing' | 'diagnosing'>('')
const transcript = ref('')
const reasoning = ref('')
const result = ref<any>(null)
let abort: (() => void) | null = null

const stageLabel = computed(() => (stage.value === 'transcribing' ? '识别手写' : '批改'))

function reset() {
  abort?.(); abort = null
  busy.value = false; stage.value = ''
  transcript.value = ''; reasoning.value = ''; result.value = null
  collected.value = 0; failure.value = null
  editing.value = false; draft.value = ''; edited.value = false
}

// 转写订正。端点让学生"看见"转写读对没有，但读错了原先只能换张照片重拍 ——
// 字迹本来就那样，重拍多少次还是认不出。现在可以直接改，改完带着订正稿重跑诊断。
const editing = ref(false)
const draft = ref('')
const edited = ref(false)   // 本次结果是基于订正稿而不是机器识别

function startEdit() {
  draft.value = transcript.value
  editing.value = true
}

function cancelEdit() {
  editing.value = false
  draft.value = ''
}

/** 用订正稿重跑诊断。跳过视觉转写这一步，所以比重拍快，也不会再认错一次。 */
function regrade() {
  const fixed = draft.value.trim()
  if (!fixed) { toast('订正后的内容不能为空', 'error'); return }
  editing.value = false
  grade(fixed)
}

function grade(useTranscript = '') {
  if (busy.value || !imageUrl.value) return
  transcript.value = ''; reasoning.value = ''; result.value = null; collected.value = 0
  failure.value = null
  busy.value = true
  // 带订正稿时后端不再认图，直接进诊断，界面上也就不该说"识别手写"
  stage.value = useTranscript ? 'diagnosing' : 'transcribing'
  abort = postSSE(
    '/grade/photo/stream',
    { image: imageUrl.value, course: courseStore.current, problem: problem.value.trim(),
      transcript: useTranscript },
    (ev: any) => {
      if (ev.type === 'transcribing') stage.value = 'transcribing'
      else if (ev.type === 'transcript') { transcript.value = ev.text; edited.value = !!ev.edited }
      else if (ev.type === 'diagnosing') stage.value = 'diagnosing'
      else if (ev.type === 'reasoning') reasoning.value += ev.text
      else if (ev.type === 'result') {
        result.value = ev
        // 诊断给了什么错因就默认选什么，学生仍可改
        collectTag.value = ev.error_tags?.[0] || ''
      } else if (ev.type === 'error') {
        failure.value = { kind: String(ev.kind || 'other'), message: sseErrorText(ev, '这次批改没有完成。') }
        if (failure.value.kind === 'not_configured') visionReady.value = false
      }
    },
    { onDone: () => { busy.value = false; stage.value = '' },
      onError: () => { busy.value = false; stage.value = '' } },
  )
}

/* ---------- 收进错题本 ---------- */
const collectKp = ref('')
const collectTag = ref('')
const collecting = ref(false)
const collected = ref(0)
const reviewRequested = ref(false)

const kpGroups = computed(() => {
  const topics = courseStore.info?.topics || []
  return topics.length ? [{ label: courseStore.info?.short || '知识点', options: topics }] : []
})
const tagGroups = computed(() =>
  (errorTags.value.length ? [{ label: '错因', options: errorTags.value.map((t) => t.name) }] : []))
const collectTagName = computed({
  get: () => errorTags.value.find((t) => t.code === collectTag.value)?.name || '',
  set: (name: string) => { collectTag.value = errorTags.value.find((t) => t.name === name)?.code || '' },
})

async function collect(requestReview = false) {
  if (collecting.value) return
  // knowledge_id 留 0 的记录会被错题本的内连接静默滤掉，所以这里必须挡住
  if (!collectKp.value.trim()) { toast('请先选择这道题属于哪个知识点', 'error'); return }
  collecting.value = true
  try {
    const r = await post<{ id: number }>('/grade/photo/collect', {
      course: courseStore.current,
      knowledge_point: collectKp.value.trim(),
      error_type: collectTag.value,
      diagnosis: result.value?.diagnosis || '',
      problem_stem: transcript.value,
      problem: problem.value.trim(),
      request_review: requestReview,
    })
    collected.value = r.id
    reviewRequested.value = requestReview
    toast(requestReview ? '已收进错题本，并请助教复核' : '已收进错题本')
  } finally {
    collecting.value = false
  }
}
</script>

<template>
  <div>
    <CLPageHeader title="拍照批改"
      sub="拍下解题草稿，转成文字后找出第一个错步和错因" />

    <div class="grid">
      <!-- ============ 左：上传 ============ -->
      <section class="cl-card upload">
        <div :class="['drop', { on: dragging, has: !!imageUrl }]"
          @click="fileInput?.click()"
          @dragover.prevent="dragging = true"
          @dragleave.prevent="dragging = false"
          @drop.prevent="onDrop">
          <img v-if="imageUrl" class="preview" :src="imageUrl" alt="草稿纸预览" />
          <div v-else class="drop-hint">
            <span class="drop-icon">＋</span>
            <b>点击选择照片</b>
            <span class="drop-sub">也可以直接拖进来，或 Ctrl+V 粘贴截图</span>
          </div>
        </div>
        <input ref="fileInput" class="file-input" type="file"
          accept="image/jpeg,image/png,image/webp" @change="onPick" />

        <CLField v-model="problem" label="题干（选填）"
          placeholder="不填也能批改；填了能让错因判断更准" />

        <div class="u-actions">
          <CLButton :disabled="!imageUrl || busy || !visionReady" @click="grade()">
            {{ busy ? '批改中…' : '开始批改' }}
          </CLButton>
          <CLButton v-if="imageUrl" variant="ghost" :disabled="busy" @click="imageUrl = ''; reset()">
            换一张
          </CLButton>
        </div>
        <p class="u-tip">照片会在本地压到最长边 1600px 再上传，字迹尽量清晰、避免反光。</p>
      </section>

      <!-- ============ 右：结果 ============ -->
      <section class="right">
        <CLThinkBlock v-if="busy" :streaming="true" :reasoning="reasoning" :label="stageLabel" />

        <div v-if="failure && !busy" class="cl-card block fail">
          <div class="b-head"><b>这次没有批完</b></div>
          <p class="b-tip">{{ failure.message }}</p>
          <div class="t-actions">
            <template v-if="failure.kind === 'not_configured'">
              <CLButton @click="goSolve(false)">手动输入题目</CLButton>
            </template>
            <template v-else-if="failure.kind === 'unreadable'">
              <CLButton @click="grade()">重新识别</CLButton>
              <CLButton variant="ghost" @click="fileInput?.click()">换一张</CLButton>
              <CLButton variant="ghost" @click="goSolve(false)">手动输入</CLButton>
            </template>
            <template v-else>
              <!-- 已经有转写稿就带着它重跑，不必再认一遍图 -->
              <CLButton @click="grade(transcript)">重试</CLButton>
              <CLButton variant="ghost" @click="goSolve()">改去解题纠错</CLButton>
            </template>
          </div>
        </div>

        <div v-if="transcript" class="cl-card block">
          <div class="b-head">
            <b>转写结果</b>
            <CLBadge :variant="edited ? 'ok' : 'cite'">{{ edited ? '你已订正' : '机器识别' }}</CLBadge>
            <CLButton v-if="!editing && !busy" variant="text" @click="startEdit">订正</CLButton>
          </div>

          <template v-if="editing">
            <CLField v-model="draft" label="" textarea
              placeholder="把认错的字改过来，其余保持原样" />
            <div class="t-actions">
              <CLButton :disabled="busy" @click="regrade">用订正稿重新批改</CLButton>
              <CLButton variant="ghost" :disabled="busy" @click="cancelEdit">取消</CLButton>
            </div>
            <p class="b-tip">只改认错的字，别顺手改正解题步骤，否则批改的就不是你的原稿了。</p>
          </template>

          <template v-else>
            <div class="transcript"><MarkdownView :source="transcript" /></div>
            <p class="b-tip">
              <code>[?]</code> 表示没认出来的字，系统不会替你猜。
              读错了点「订正」直接改，不必重拍。
            </p>
          </template>
        </div>

        <div v-if="result" class="result" :class="result.correct ? 'ok' : 'bad'">
          <div class="r-head">
            <CLBadge :variant="result.correct ? 'ok' : 'err'">{{ result.correct ? '步骤无误' : '发现问题' }}</CLBadge>
            <CLBadge v-for="(n, i) in result.error_tag_names" :key="i" variant="warn">{{ n }}</CLBadge>
          </div>
          <div v-if="result.diagnosis" class="r-text"><MarkdownView :source="result.diagnosis" /></div>
          <div v-if="result.remediation" class="r-text">
            <b>下一步：</b><MarkdownView :source="result.remediation" />
          </div>
          <div class="t-actions">
            <CLButton variant="ghost" @click="goSolve()">带着这份解法去解题纠错</CLButton>
          </div>
        </div>

        <!-- 人在环：确认后才入库 -->
        <div v-if="result && !result.correct" class="cl-card collect">
          <div class="b-head"><b>收进错题本</b></div>
          <p class="c-tip">确认转写和错因都对，再收进去。存错了会连带影响薄弱画像和复习计划。</p>
          <div class="c-form">
            <CLSelect v-model="collectKp" label="知识点" :groups="kpGroups" placeholder="选择或直接输入" />
            <CLSelect v-model="collectTagName" label="错因" :groups="tagGroups" placeholder="选择错因" />
          </div>
          <div class="c-actions">
            <CLButton :disabled="collecting || !!collected" @click="collect(false)">
              {{ collected ? '已收录' : (collecting ? '收录中…' : '确认收录') }}
            </CLButton>
            <CLButton v-if="!collected" variant="ghost" :disabled="collecting" @click="collect(true)">
              觉得批得不对？收录并请助教看看
            </CLButton>
            <span v-if="collected" class="c-done">
              已进入错题本，复习计划会排上这个知识点。<template v-if="reviewRequested">助教复核后，结论会显示在错题本这条记录下面。</template>
            </span>
          </div>
        </div>

        <!-- 空闲时不只放一句"还没有结果"：把批改会产出的三样东西先摆出来，
             右栏和左边上传卡等高，页面不会一边满一边空。没开通时也在这里说，不另起横幅。 -->
        <div v-if="!busy && !transcript && !result && !failure" class="cl-card idle">
          <div class="i-head">
            <b>{{ visionReady ? (imageUrl ? '照片已就绪' : '还没有批改结果') : '拍照批改暂未开通' }}</b>
            <CLBadge v-if="!visionReady" variant="warn">未开通</CLBadge>
          </div>
          <p class="i-sub">
            <template v-if="!visionReady">可以先把题目和你的解法手动输入，到「解题纠错」里逐步批改，批改结果是同一套。</template>
            <template v-else-if="imageUrl">点「开始批改」，结果会按下面的顺序出现在这里。</template>
            <template v-else>先选一张草稿纸照片，点「开始批改」，结果会按下面的顺序出现在这里。</template>
          </p>

          <ol class="steps">
            <li>
              <span class="s-no">1</span>
              <div class="s-body">
                <b>转写手写</b>
                <span>把草稿纸认成文字和公式；认不出的字标 <code>[?]</code>，读错了可以直接订正，不必重拍。</span>
              </div>
            </li>
            <li>
              <span class="s-no">2</span>
              <div class="s-body">
                <b>找第一个错步</b>
                <span>从头核对每一步，停在第一处出错的地方，给出错因标签和下一步该怎么改。</span>
              </div>
            </li>
            <li>
              <span class="s-no">3</span>
              <div class="s-body">
                <b>收进错题本</b>
                <span>你确认转写和错因都对，才会入库；之后的薄弱画像和复习计划会排上这个知识点。</span>
              </div>
            </li>
          </ol>

          <div v-if="!visionReady" class="t-actions">
            <CLButton @click="goSolve(false)">手动输入题目</CLButton>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.fail { border-left: 3px solid var(--cl-warn); }

.grid { display: grid; grid-template-columns: minmax(0, 380px) minmax(0, 1fr); gap: 18px; align-items: start; }
@media (max-width: 1080px) { .grid { grid-template-columns: minmax(0, 1fr); } }

/* ---------- 上传区 ---------- */
.upload { display: flex; flex-direction: column; gap: 14px; align-self: start; }
/* 投放区比卡面更暗：凹槽感=可放东西进去，提亮反而会浮起来抢注意力 */
.drop {
  position: relative; display: flex; align-items: center; justify-content: center;
  min-height: 220px; padding: 14px; cursor: pointer;
  border: 1px dashed var(--cl-row-border); border-radius: 14px;
  background: var(--cl-inset);
  transition: border-color .18s ease, background .18s ease, box-shadow .18s ease;
}
.drop:hover, .drop.on {
  border-color: var(--cl-tint-border-strong);
  background: var(--cl-inset-focus);
  box-shadow: inset 0 0 30px -18px var(--cl-brand);
}
.drop.has { border-style: solid; }
.preview { max-width: 100%; max-height: 340px; border-radius: 10px; display: block; }
.drop-hint { display: flex; flex-direction: column; align-items: center; gap: 6px; text-align: center; color: var(--cl-text-3); }
.drop-icon { font-size: 26px; line-height: 1; color: var(--cl-text-3); }
.drop-hint b { color: var(--cl-text-2); font-size: 14px; }
.drop-sub { font-size: 12px; }
.file-input { display: none; }
.u-actions { display: flex; gap: 10px; flex-wrap: wrap; }
.u-tip { margin: 0; font-size: 12px; color: var(--cl-text-3); line-height: 1.6; }

/* ---------- 结果区 ---------- */
/* 右栏拉满整行高度，空闲卡才能和左边上传卡底边对齐 */
.right { display: flex; flex-direction: column; gap: 14px; min-width: 0; align-self: stretch; }

/* ---------- 空闲 ---------- */
.idle { flex: 1; display: flex; flex-direction: column; gap: 10px; }
.i-head { display: flex; align-items: center; gap: 10px; }
.i-head b { font-size: 15px; color: var(--cl-text); }
.i-sub { margin: 0; font-size: 13px; color: var(--cl-text-3); line-height: 1.7; }
.steps { list-style: none; margin: 6px 0 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }
.steps li {
  display: flex; gap: 12px; align-items: flex-start;
  padding: 12px 14px; border-radius: 12px;
  background: var(--cl-inset); border: 1px solid var(--cl-row-border);
}
.s-no {
  flex: none; width: 24px; height: 24px; border-radius: 50%;
  display: grid; place-items: center; font-size: 12px; font-weight: 700;
  color: var(--cl-brand); background: var(--cl-tint); border: 1px solid var(--cl-tint-border);
}
.s-body { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.s-body b { font-size: 13.5px; color: var(--cl-text); }
.s-body span { font-size: 12.5px; color: var(--cl-text-3); line-height: 1.7; }
.s-body code { font-size: 11px; }
/* 没开通时步骤只是"将来会这样"，压暗一档，别让人以为能用 */
.idle:has(.t-actions) .steps { opacity: .7; }
.block { display: flex; flex-direction: column; gap: 10px; }
.b-head { display: flex; align-items: center; gap: 10px; }
.b-head b { font-size: 14px; color: var(--cl-text); }
/* 转写文本也是"原材料"而非结论，同样压暗 */
.t-actions { display: flex; gap: 8px; margin-top: 10px; }
.transcript {
  padding: 12px 14px; border-radius: 10px;
  background: var(--cl-inset); border: 1px solid var(--cl-row-border);
  font-size: 13px; line-height: 1.8; color: var(--cl-text-2);
  max-height: 420px; overflow: auto;
}
.b-tip { margin: 0; font-size: 12px; color: var(--cl-text-3); line-height: 1.6; }
.b-tip code { font-size: 11px; }

.result {
  padding: 16px 18px; border-radius: 14px;
  border: 1px solid var(--cl-glass-border); border-left-width: 3px;
  display: flex; flex-direction: column; gap: 10px;
}
/* 判错用琥珀不用红：做错是学习的常态，不该被当成报错 */
.result.bad {
  border-left-color: var(--cl-warn);
  background: linear-gradient(180deg, var(--cl-warn-tint), transparent 70%);
  box-shadow: 0 0 26px -16px var(--cl-warn);
}
.result.ok {
  border-left-color: var(--cl-ok);
  background: linear-gradient(180deg, var(--cl-ok-tint), transparent 70%);
  box-shadow: 0 0 26px -16px var(--cl-ok);
}
.r-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.r-text { font-size: 13.5px; line-height: 1.8; color: var(--cl-text-2); }

/* ---------- 收录 ---------- */
.collect { display: flex; flex-direction: column; gap: 12px; }
.c-tip { margin: 0; font-size: 12px; color: var(--cl-text-3); line-height: 1.6; }
.c-form { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; }
.c-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.c-done { font-size: 12px; color: var(--cl-ok); }
</style>
