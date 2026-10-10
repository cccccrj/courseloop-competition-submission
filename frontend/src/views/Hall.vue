<script setup lang="ts">
import { ref, nextTick, computed, watch, onActivated } from 'vue'
import { useCourseStore } from '@/stores/course'
import { useChatStore, type ChatMsg, type SessionBrief } from '@/stores/chat'
import { post } from '@/api/client'
import { toast } from '@/api/toast'
import MarkdownView from '@/components/MarkdownView.vue'
import CLPageHeader from '@/components/ui/CLPageHeader.vue'
import CLButton from '@/components/ui/CLButton.vue'
import CLBadge from '@/components/ui/CLBadge.vue'
import CLEmpty from '@/components/ui/CLEmpty.vue'
import CLThinkBlock from '@/components/ui/CLThinkBlock.vue'
import CLCitations from '@/components/ui/CLCitations.vue'
import { cleanAnswer } from '@/utils/sanitize'

const courseStore = useCourseStore()
const chat = useChatStore()
courseStore.load()

// 会话状态住在 store 里（见 stores/chat.ts）：切页、刷新、重登都不影响它，
// 这个组件只负责画。原先这些是组件内的 ref，只靠 keep-alive 保着，
// 刷新一下正在生成的回答就没了。
const conv = computed(() => chat.conv(courseStore.current))
const messages = computed(() => conv.value.messages)
const streaming = computed(() => conv.value.streaming)
const input = computed({
  get: () => conv.value.draft,
  set: (v: string) => chat.setDraft(courseStore.current, v),
})

const scroller = ref<HTMLElement | null>(null)
const composerEl = ref<HTMLTextAreaElement | null>(null)

// 点推荐问题 → 填进输入框并聚焦（可再编辑后发送），不用自己打
function pick(s: string) {
  input.value = s
  nextTick(() => composerEl.value?.focus())
}

// 数据结构兜底示例（后端 /courses 未就绪或该门无 sample_questions 时用）
const DS_POOL = [
  '什么时候该用栈、什么时候该用队列？',
  '讲讲二叉树的三种遍历，最好带例子',
  '快速排序最坏情况复杂度是多少，为什么？',
  '哈希表是怎么解决冲突的？',
  '二分查找的前提条件和时间复杂度是什么？',
  '图的 BFS 和 DFS 分别适合什么场景？',
]
const pool = computed(() => {
  const q = courseStore.info?.sample_questions
  return q && q.length ? q : (courseStore.current === 'ds-algo' ? DS_POOL : [])
})

const samples = ref<string[]>([])
function shuffleSamples() {
  samples.value = [...pool.value].sort(() => Math.random() - 0.5).slice(0, 3)
}
shuffleSamples()
// 切科目：换一份示例并读回那门自己的对话。**不再清空** ——
// 每门各存各的会话（stores/chat.ts 按 course 分开），
// 原先"看一眼计组"就把数据结构那段问答烧掉，切回来只剩空白。
watch(() => courseStore.current, (c) => { shuffleSamples(); chat.restore(c) })
watch(pool, () => { if (!samples.value.length) shuffleSamples() })

// 进页面时把上次的对话接回来（onActivated 首次挂载也会触发）
onActivated(() => chat.restore(courseStore.current))

async function scrollDown() {
  await nextTick()
  scroller.value?.scrollTo({ top: scroller.value.scrollHeight, behavior: 'smooth' })
}
watch(() => messages.value.length, scrollDown)
// 流式期间内容在原地长，长度不变，所以还要盯住最后一条的字数
watch(() => messages.value[messages.value.length - 1]?.content.length, scrollDown)

function send(text?: string) {
  chat.send(courseStore.current, text ?? input.value)
}
function stop() { chat.stop(courseStore.current) }

// 请助教看看（docs/35 P1-2）。AI 自己没检索到教材依据时，这个出口要更显眼：
// 那种回答只是模型的一家之言，学生应当知道可以找真人确认。
const requesting = ref(-1)
async function requestReview(i: number) {
  const m = messages.value[i] as ChatMsg
  if (!m || m.reviewRequestId || requesting.value >= 0) return
  const q = [...messages.value.slice(0, i)].reverse().find((x) => x.role === 'user')
  requesting.value = i
  try {
    const r: any = await post('/review-requests', {
      course: courseStore.current, source: 'hall', question: q?.content || '',
      ai_answer: cleanAnswer(m.content), no_evidence: m.verified === false,
    })
    m.reviewRequestId = r.id
    chat.persist(true)
    toast('已提交给助教。回复会出现在「错题与学习记录」里')
  } finally {
    requesting.value = -1
  }
}
function retry() { chat.retry(courseStore.current) }

// ---------- 历史会话 ----------
const historyOpen = ref(false)
const historyItems = ref<SessionBrief[]>([])
const historyLoading = ref(false)

async function toggleHistory() {
  historyOpen.value = !historyOpen.value
  if (!historyOpen.value) return
  historyLoading.value = true
  try {
    historyItems.value = await chat.history(courseStore.current)
  } finally {
    historyLoading.value = false
  }
}
async function openSession(id: number) {
  historyOpen.value = false
  await chat.open(courseStore.current, id)
  scrollDown()
}
async function removeSession(id: number) {
  await chat.remove(courseStore.current, id)
  historyItems.value = historyItems.value.filter((s) => s.id !== id)
}
function newChat() {
  historyOpen.value = false
  chat.reset(courseStore.current)
}
</script>

<template>
  <div class="hall">
    <CLPageHeader title="学习大厅" sub="向 AI 助手提问，找得到教材依据的回答会附上原文">
      <template #actions>
        <CLBadge variant="cite">当前科目：{{ courseStore.info?.short }}</CLBadge>
        <!-- 对话现在存在库里，所以这里能给出"回到上一次"的入口 —— 这是
             /sessions 读回接口唯一的用户可见出口，没有它，历史等于不存在。 -->
        <div class="hist-wrap">
          <button class="hist-btn" :class="{ on: historyOpen }" @click="toggleHistory">历史对话</button>
          <div v-if="historyOpen" class="hist-pop">
            <div class="hist-head">
              <span>{{ courseStore.info?.short }} · 最近的对话</span>
              <button class="hist-new" @click="newChat">开新对话</button>
            </div>
            <div v-if="historyLoading" class="hist-empty">读取中…</div>
            <div v-else-if="!historyItems.length" class="hist-empty">还没有历史对话</div>
            <ul v-else class="hist-list">
              <li v-for="h in historyItems" :key="h.id" :class="{ cur: h.id === conv.sessionId }">
                <button class="hist-item" @click="openSession(h.id)">
                  <span class="hist-title">{{ h.title || '（空对话）' }}</span>
                  <span class="hist-meta">{{ h.turns }} 轮 · {{ (h.last_at || h.started_at || '').slice(5, 16) }}</span>
                </button>
                <button class="hist-del" title="删除这次对话" @click.stop="removeSession(h.id)">×</button>
              </li>
            </ul>
          </div>
        </div>
      </template>
    </CLPageHeader>

    <div ref="scroller" class="stream">
      <CLEmpty v-if="messages.length === 0" title="开始学习" hint="拿不准的回答，可以请助教看看">
        <div class="samples">
          <transition-group name="sample" tag="div" class="sample-list">
            <button v-for="s in samples" :key="s" class="sample" @click="pick(s)">{{ s }}</button>
          </transition-group>
          <button class="shuffle" title="换一批推荐问题" @click="shuffleSamples">
            <svg viewBox="0 0 24 24" class="ic"><path d="M17 3l4 4-4 4" /><path d="M21 7H8a4 4 0 0 0-4 4" /><path d="M7 21l-4-4 4-4" /><path d="M3 17h13a4 4 0 0 0 4-4" /></svg>
            换一批
          </button>
        </div>
      </CLEmpty>

      <div v-for="(m, i) in messages" :key="i" :class="['bubble-row', m.role]">
        <div :class="['bubble', m.role]">
          <template v-if="m.role === 'assistant'">
            <CLThinkBlock v-if="m.reasoning || m.thinking" class="msg-think" :streaming="m.thinking" :reasoning="m.reasoning" label="思考" />
            <MarkdownView v-if="cleanAnswer(m.content)" :source="cleanAnswer(m.content)" />
          </template>
          <template v-else>{{ m.content }}</template>

          <div v-if="m.role === 'assistant' && (m.citations?.length || m.verified !== null)" class="meta">
            <div v-if="m.verified !== null" class="verification">
              <CLBadge v-if="m.verified === true" variant="ok">已检索教材依据</CLBadge>
              <CLBadge v-else variant="warn">没找到教材依据，仅供参考</CLBadge>
            </div>
            <CLCitations v-if="m.citations?.length" :citations="m.citations" />
          </div>

          <!-- 人工出口。只挂在已经写完的回答上：流式中途点它，交上去的是半句话 -->
          <div v-if="m.role === 'assistant' && m.content && !(streaming && i === messages.length - 1)"
            class="ask-ta" :class="{ strong: m.verified === false }">
            <span v-if="m.reviewRequestId" class="ask-done">已请助教复核，回复会出现在「错题与学习记录」</span>
            <CLButton v-else :variant="m.verified === false ? 'ghost' : 'text'"
              :disabled="requesting === i" @click="requestReview(i)">
              {{ m.verified === false ? '依据不足，请助教确认' : '拿不准？请助教看看' }}
            </CLButton>
          </div>

          <!-- 被打断的半截回答。刷新、断网、后端重启都会走到这里：内容留着（写了
               一半也有用），但要说清楚它没写完，并且给一个一键重来的出口。 -->
          <div v-if="m.interrupted && i === messages.length - 1 && !streaming" class="broken">
            <CLBadge variant="warn">回答未生成完</CLBadge>
            <CLButton variant="text" @click="retry">重新生成</CLButton>
          </div>
        </div>
      </div>
    </div>

    <div class="composer">
      <textarea
        ref="composerEl"
        class="cl-textarea"
        v-model="input"
        placeholder="输入你的问题，Enter 发送，Shift+Enter 换行"
        @keydown.enter.exact.prevent="send()"
      />
      <div class="composer-actions">
        <CLButton v-if="streaming" variant="ghost" @click="stop">停止</CLButton>
        <CLButton v-else :disabled="!input.trim()" @click="send()">发送</CLButton>
      </div>
    </div>
  </div>
</template>

<style scoped>
.hall { display: flex; flex-direction: column; height: 100%; max-width: 900px; margin: 0 auto; }
.head { display: flex; justify-content: space-between; align-items: flex-start; }
.stream { flex: 1; overflow: auto; padding: 8px 2px 16px; display: flex; flex-direction: column; gap: 16px; }
.empty { text-align: center; color: var(--cl-text-2); margin-top: 10%; }
.empty-title { font-size: 18px; font-weight: 700; color: var(--cl-text); margin-bottom: 20px; }
.samples { display: flex; flex-direction: column; gap: 12px; align-items: center; margin-top: 6px; }
.sample-list { display: flex; flex-direction: column; gap: 10px; align-items: center; }
/* 空状态下这三颗胶囊是页面上唯一可点的东西，要明显但不能盖过标题：
   常态是安静的玻璃，hover 才亮起靛蓝描边和辉光。 */
.sample {
  border: 1px solid var(--cl-glass-border); background: var(--cl-glass-2); cursor: pointer;
  padding: 10px 18px; border-radius: var(--cl-radius-pill); font: inherit; color: var(--cl-text);
  box-shadow: var(--cl-shadow-md);
  transition: transform .14s ease, box-shadow .14s ease, color .14s ease, border-color .14s ease, background .14s ease;
}
.sample:hover {
  transform: translateY(-1px); color: var(--cl-brand-700);
  background: var(--cl-tint); border-color: var(--cl-tint-border-strong);
  box-shadow: var(--cl-glow);
}
.sample:focus-visible { outline: 2px solid var(--cl-brand); outline-offset: 2px; }
.shuffle {
  display: inline-flex; align-items: center; gap: 6px; margin-top: 2px;
  border: none; background: transparent; cursor: pointer; font: inherit; font-size: 13px; font-weight: 600;
  color: var(--cl-text-3); border-radius: var(--cl-radius-pill); padding: 5px 10px;
  transition: color .12s ease, background .12s ease;
}
.shuffle:hover { color: var(--cl-brand-600); background: var(--cl-tint); }
.ask-ta { display: flex; justify-content: flex-end; margin-top: 6px; }
.ask-done { font-size: 12px; color: var(--cl-text-3); }
.shuffle .ic { width: 15px; height: 15px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
.sample-enter-active, .sample-leave-active { transition: opacity .2s ease, transform .2s ease; }
.sample-enter-from { opacity: 0; transform: translateY(6px); }
.sample-leave-to { opacity: 0; transform: translateY(-6px); position: absolute; }

/* ---------- 对话气泡 ---------- */
/* 两侧用**两种材质**而不是两种颜色区分：
   用户是实心的品牌渐变（自己说的话，确定的），助教是半透明玻璃（生成的内容，
   带引用和验证标记）。深色下只改色相很容易两边都糊成一片深蓝。 */
.bubble-row { display: flex; gap: 10px; align-items: flex-start; }
.bubble-row.user { justify-content: flex-end; }
.bubble {
  position: relative; max-width: 82%; padding: 12px 16px;
  border-radius: var(--cl-radius); box-shadow: var(--cl-shadow-md);
}
.bubble.user {
  background: var(--cl-brand-grad); color: var(--cl-on-brand);
  border-bottom-right-radius: 6px;
  box-shadow: var(--cl-glow);
  white-space: pre-wrap;
}
.bubble.assistant {
  background: var(--cl-glass); border: 1px solid var(--cl-glass-border);
  border-bottom-left-radius: 6px;
  box-shadow: var(--cl-glass-shadow);
}
/* 助教气泡的顶缘高光，与卡片同一手法 */
.bubble.assistant::before {
  content: '';
  position: absolute; left: 16px; right: 16px; top: 0; height: 1px;
  background: var(--cl-glass-hi); pointer-events: none;
}
.msg-think { margin-bottom: 10px; }
.broken { display: flex; align-items: center; gap: 8px; margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--cl-row-border); }

/* ---------- 历史对话浮层 ---------- */
/* 贴着标题栏右侧展开，和顶栏的科目切换器共用同一套玻璃、圆角与 hover 语言。 */
.hist-wrap { position: relative; }
.hist-btn {
  height: 30px; padding: 0 12px; font: inherit; font-size: 13px; font-weight: 600; cursor: pointer;
  color: var(--cl-text-2); background: var(--cl-glass-2); border: 1px solid var(--cl-glass-border);
  border-radius: var(--cl-radius-pill); transition: color .12s ease, border-color .12s ease, background .12s ease;
}
.hist-btn:hover, .hist-btn.on { color: var(--cl-brand-600); background: var(--cl-tint); border-color: var(--cl-tint-border-strong); }
.hist-pop {
  position: absolute; right: 0; top: calc(100% + 8px); z-index: 20; width: 320px;
  background: var(--cl-glass); border: 1px solid var(--cl-glass-border);
  border-radius: var(--cl-radius); box-shadow: var(--cl-shadow-lg, var(--cl-shadow-md));
  backdrop-filter: blur(12px); overflow: hidden;
}
.hist-head {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 10px 12px; font-size: 12px; color: var(--cl-text-3);
  border-bottom: 1px solid var(--cl-row-border);
}
.hist-new { border: none; background: transparent; cursor: pointer; font: inherit; font-size: 12px; font-weight: 700; color: var(--cl-brand-600); }
.hist-empty { padding: 18px 12px; text-align: center; font-size: 13px; color: var(--cl-text-3); }
.hist-list { list-style: none; margin: 0; padding: 4px; max-height: 320px; overflow: auto; }
.hist-list li { display: flex; align-items: center; gap: 4px; border-radius: 8px; }
.hist-list li:hover { background: var(--cl-tint); }
.hist-list li.cur { background: var(--cl-tint); }
.hist-item {
  flex: 1; display: flex; flex-direction: column; gap: 2px; align-items: flex-start;
  padding: 8px 10px; border: none; background: transparent; cursor: pointer; font: inherit; text-align: left;
}
.hist-title { font-size: 13px; color: var(--cl-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 240px; }
.hist-meta { font-size: 11px; color: var(--cl-text-3); }
.hist-del {
  flex-shrink: 0; width: 26px; height: 26px; margin-right: 6px; border: none; background: transparent;
  cursor: pointer; font-size: 16px; line-height: 1; color: var(--cl-text-3); border-radius: 6px;
}
.hist-del:hover { color: var(--cl-danger, #e5484d); background: var(--cl-glass-2); }
.meta { display: grid; gap: 8px; margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--cl-row-border); }
.verification { display: flex; flex-wrap: wrap; gap: 6px; }

/* ---------- 输入区 ---------- */
/* 这里**不需要** backdrop-filter：.stream 自己是滚动容器，内容被它裁掉，
   不会滚到输入区下面去，加模糊只是白花一个合成层。
   分隔靠一条发丝线 + 上缘的一点提亮，表示"这是一块固定的台面"。 */
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
.composer .cl-textarea { min-height: 56px; }
.composer-actions { flex-shrink: 0; }
</style>
