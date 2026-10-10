import { defineStore } from 'pinia'
import { API_BASE, authHeader, get, post } from '@/api/client'
import { postSSE, sseErrorText } from '@/api/sse'
import { toast } from '@/api/toast'
import type { Citation } from '@/api/types'
import { onPageExit, useSnapshot } from './snapshot'

/**
 * 真题模考的会话状态。
 *
 * 模考比大厅问答难续的地方在于：**它有一道关不上的闸**。交卷之后作答再也改不了，
 * 而分数还要几十次判题才算得出来 —— 中间断掉的那几分钟，是这个应用里代价最高的
 * 一次中断。所以这里要接回来的不是一段对话，是三种被打断的处境：
 *
 *   1. **考试中断了**（刷新 / 关页面 / 换设备）。后端一直存着卷子和暂存的作答，
 *      倒计时也按开考时刻从服务端算 —— 这一条原先就能接回来。缺的是**没翻页就
 *      刷新的那道题**：暂存只在翻题时发生，综合题写十分钟没翻页，刷新就没了。
 *      现在改成节流暂存 + 退出前强制刷，并且停在第几题也一起记下来。
 *   2. **阅卷断了**。交卷闸已经落下，成绩单还没生成。这一场在旧版里就此锁死：
 *      重发交卷一律 409，`/exam/result` 只能给出一份 0 分成绩单，而错题本里已经
 *      躺着批过那几道题的记录。现在后端边批边落、可以续阅，前端开局看到
 *      `grading_pending` 就自动接着批完。
 *   3. **成绩单看完刷新了**。成绩单一直在库里，但页面刷新后回到开考前 —— 记住
 *      最近一次的 session_id，回来直接拉回那份成绩单。
 *
 * 快照只存**本地才有的东西**：停在第几题、还没暂存上去的作答、最近看的成绩单 id。
 * 卷子、批改结果、成绩单都从后端读 —— 它们在库里是权威的，抄一份到 sessionStorage
 * 既大又会过期。
 */

export interface ExamItem {
  seq: number; problem_id: number; course: string; section: 'choice' | 'comprehensive'
  score: number; knowledge: string; chapter: string; difficulty: number
  verification_type: string; stem: string; type: string; func_name: string
  answer_kind: string; answer_unit: string; answer_multi: boolean; test_count: number
  is_past_paper?: boolean
}

/** 批改后的题：后端把逐题结果并进题面，score 被覆盖成**实得分**，满分挪到 full。 */
export interface GradedItem extends ExamItem {
  full: number; your_answer: string; passed: boolean; skipped: boolean; ratio: number | null
  details: string; diagnosis: string; reference: string; error_tag_names?: string[]
  citations?: Citation[]
}

export interface ExamSection {
  course: string; short: string; score: number
  choice_count: number; choice_got: number
  comprehensive_count: number; comprehensive_got: number; comprehensive_score: number
}
export interface Shortfall { course: string; short: string; section: string; got: number; want: number }

export interface ExamReport {
  score: number; full_score: number; standard_score: number; rate: number; scaled_score: number
  sections: Record<string, { score: number; full: number; rate: number; count: number; correct: number }>
  courses: { course: string; short: string; score: number; full: number; rate: number }[]
  weak_chapters: { course: string; chapter: string; score: number; full: number; rate: number }[]
  answered: number; correct: number; count: number
}

export interface ExamState {
  session_id: number; started_at: string; submitted: boolean
  duration_minutes: number; remaining_seconds: number
  full_score: number; standard_score: number
  sections: ExamSection[]; shortfall: Shortfall[]
  items: ExamItem[]; answers: Record<string, string>
}

/** 交了卷但没阅完的那场。 */
export interface GradingPending { session_id: number; submitted_at: string; graded: number; total: number }

export interface ExamPreview {
  ready: boolean; min_items: number; full_score: number; standard_score: number
  past_paper_count: number
  duration_minutes: number; structure: string
  sections: ExamSection[]; shortfall: Shortfall[]
  pending: ExamState | null
  grading_pending: GradingPending | null
}

export interface ExamHistoryRow {
  session_id: number; started_at: string; submitted_at: string; state: string
  graded: boolean; score: number | null; full_score: number | null
  scaled_score: number | null; standard_score: number
}

/** 快照里存的：本地才有的那点东西。 */
interface ExamCache {
  sessionId: number
  cursor: number
  answers: Record<number, string>
  /** 最近一次看的成绩单，刷新后照着它把成绩单拉回来。 */
  resultId: number
}

const snapshot = useSnapshot<ExamCache>('cl_exam_v1')

function emptyCache(): ExamCache {
  return { sessionId: 0, cursor: 0, answers: {}, resultId: 0 }
}

/** 作答暂存到后端的节流间隔。比快照那 1 秒长：这一条走网络，丢了还有本地快照垫着。 */
const FLUSH_MS = 3000

/**
 * 关标签页时最多还能带走多大的一份作答。
 *
 * `keepalive` 的请求体规范上限是 64KB，超了浏览器直接拒发。代码题一道能写四千字，
 * 整卷堆起来是可能超的 —— 超了就不发，回到"本地快照留着、下次进页面补暂存"，
 * 而不是发一个必然失败的请求然后以为存上了。
 */
const BEACON_MAX = 60_000

// 中断函数与计时器不进 state：不可序列化，也不该被响应式代理包一层。
let stopStream: (() => void) | null = null
let ticker: ReturnType<typeof setInterval> | null = null
let flushTimer: ReturnType<typeof setTimeout> | undefined
let exitHooked = false

export const useExamStore = defineStore('exam', {
  state: () => ({
    loading: true,
    busy: false,
    preview: null as ExamPreview | null,
    exam: null as ExamState | null,
    answers: {} as Record<number, string>,
    cursor: 0,
    remaining: 0,
    grading: null as { done: number; total: number } | null,
    report: null as ExamReport | null,
    reviewItems: [] as GradedItem[],
    /** 正在看的那份成绩单属于哪一场，刷新后靠它拉回来。 */
    resultId: 0,
    cache: (snapshot.read() ?? emptyCache()) as ExamCache,
  }),

  getters: {
    current: (s): ExamItem | null => s.exam?.items[s.cursor] || null,
    answeredCount: (s): number =>
      Object.values(s.answers).filter((v) => (v || '').trim()).length,
  },

  actions: {
    persist(immediate = false) {
      this.hookExit()
      this.cache = {
        sessionId: this.exam?.session_id || 0,
        cursor: this.cursor,
        answers: this.answers,
        resultId: this.resultId,
      }
      snapshot.write(() => this.cache, immediate)
    },

    /** 页面要走了：本地快照强制落盘，顺便把还没暂存的作答送到后端。 */
    hookExit() {
      if (exitHooked) return
      exitHooked = true
      onPageExit(() => {
        this.persist(true)
        this.beaconSave()
      })
    },

    /**
     * 关页面这一刻把作答送出去。
     *
     * 光有本地快照不够：那份只活在这个标签页里，考生在手机上关掉、换电脑接着考，
     * 最后写的那道题就没了 —— 而模考是限时的，没有第二次机会补写。
     * 用 `keepalive` 而不是 sendBeacon：身份是 Authorization 头里的签名令牌，
     * beacon 发不了自定义头，到后端就是个 401。
     */
    beaconSave() {
      if (!flushTimer) return              // 没有未暂存的改动
      clearTimeout(flushTimer)
      flushTimer = undefined
      const exam = this.exam
      if (!exam || exam.submitted) return
      const body = JSON.stringify({ session_id: exam.session_id, answers: this.answers })
      if (body.length > BEACON_MAX) return
      try {
        void fetch(`${API_BASE}/exam/save`, {
          method: 'POST', keepalive: true, body,
          headers: { 'Content-Type': 'application/json', ...authHeader() },
        }).catch(() => { /* best-effort：本地快照仍然留着 */ })
      } catch {
        /* 同上 */
      }
    },

    /* ---------- 开局：这三种处境要分开认 ---------- */
    async load() {
      this.loading = true
      try {
        const p = await get<ExamPreview>('/exam/preview')
        this.preview = p
        if (p.pending) {
          this.enter(p.pending)
        } else if (p.grading_pending) {
          // 交了卷、没阅完。不接着批的话，这一场在考生眼里就是凭空消失了。
          this.exam = null
          this.stopTicker()
          await this.resumeGrading(p.grading_pending)
        } else {
          this.exam = null
          this.stopTicker()
          // 刷新前正在看成绩单，就把它拉回来（成绩单在库里，本地只记 id）
          if (!this.report && this.cache.resultId) await this.openResult(this.cache.resultId)
        }
      } finally {
        this.loading = false
      }
    },

    /** 进入一场考试：服务端作答打底，本地那份没暂存上去的覆盖在上面。 */
    enter(state: ExamState) {
      this.exam = state
      this.report = null
      this.reviewItems = []
      this.resultId = 0
      const merged: Record<number, string> = {}
      for (const [seq, text] of Object.entries(state.answers || {})) merged[Number(seq)] = text
      // 同一场才叠：本地快照是这个标签页上一次加载留下的，换了一场就跟这张卷子无关了。
      if (this.cache.sessionId === state.session_id) {
        // 本地的更新 —— 它正是"写了还没来得及暂存"的那部分，服务端不可能有。
        for (const [seq, text] of Object.entries(this.cache.answers || {})) {
          if ((text || '').trim()) merged[Number(seq)] = text
        }
        this.cursor = Math.min(Math.max(0, this.cache.cursor), Math.max(0, state.items.length - 1))
      } else {
        this.cursor = 0
      }
      this.answers = merged
      this.remaining = state.remaining_seconds
      this.persist(true)
      // 本地比服务端多出来的那部分，立刻补暂存上去，别让它只活在这个标签页里
      void this.flush()
      this.startTicker()
    },

    async begin() {
      if (this.busy) return
      this.busy = true
      try {
        this.enter(await post<ExamState>('/exam/start'))
      } catch {
        /* 组不出卷时后端返回 503 + 说明，全局拦截器已经提示过 */
      } finally {
        this.busy = false
      }
    },

    /* ---------- 倒计时 ---------- */
    stopTicker() {
      if (ticker) { clearInterval(ticker); ticker = null }
    },

    startTicker() {
      this.stopTicker()
      ticker = setInterval(() => {
        if (this.remaining > 0) { this.remaining -= 1; return }
        // 时间到就自动交卷。限时考试里"到点了还能接着写"没有意义，
        // 而已作答的内容都在服务端，自动交比让人白写更好。
        //
        // 计时器长在 store 上而不是组件里：考生切到错题本去查个东西，
        // 考试的钟不该跟着停 —— 它本来就是按服务端的开考时刻算的。
        this.stopTicker()
        if (this.exam && !this.busy) {
          toast('时间到，已自动交卷', 'info')
          void this.submit(true)
        }
      }, 1000)
    },

    /* ---------- 作答 ---------- */
    setAnswer(seq: number, text: string) {
      this.answers[seq] = text
      this.persist()
      // 节流暂存：不等翻题。综合题写十分钟不翻页是常事，而刷新就发生在那十分钟里。
      this.scheduleFlush()
    },

    scheduleFlush() {
      if (flushTimer) return
      flushTimer = setTimeout(() => { flushTimer = undefined; void this.flush() }, FLUSH_MS)
    },

    /** 把作答暂存到后端。失败不打断考试 —— 交卷时还会整份再传一次。 */
    async flush() {
      if (flushTimer) { clearTimeout(flushTimer); flushTimer = undefined }
      const exam = this.exam
      if (!exam || exam.submitted) return
      const payload: Record<string, string> = {}
      for (const [seq, text] of Object.entries(this.answers)) payload[seq] = text
      if (!Object.keys(payload).length) return
      try {
        await post('/exam/save', { session_id: exam.session_id, answers: payload })
      } catch {
        /* 断网时就靠本地快照顶着，回来再暂存 */
      }
    },

    async go(index: number) {
      if (!this.exam) return
      const next = Math.max(0, Math.min(this.exam.items.length - 1, index))
      if (next === this.cursor) return
      this.cursor = next
      this.persist(true)
      await this.flush()          // 翻题就存一次：中途断线，已答的题还在
    },

    /* ---------- 交卷与阅卷 ---------- */
    async submit(auto = false) {
      const exam = this.exam
      if (!exam || this.busy) return
      if (!auto && this.answeredCount < exam.items.length &&
          !window.confirm(`还有 ${exam.items.length - this.answeredCount} 题没作答，确定交卷？`)) return
      this.busy = true
      await this.flush()
      this.stopTicker()
      this.grading = { done: 0, total: exam.items.length }
      await this.runGrading(exam.session_id)
    },

    /** 接着批上一次没批完的那场。与交卷走同一个端点：后端认得出该续哪几道题。 */
    async resumeGrading(row: GradingPending) {
      if (this.busy) return
      this.busy = true
      this.grading = { done: row.graded, total: row.total }
      toast('上次阅卷没走完，正在接着批', 'info')
      await this.runGrading(row.session_id)
    },

    runGrading(sessionId: number): Promise<void> {
      return new Promise((resolve) => {
        stopStream = postSSE('/exam/submit/stream', { session_id: sessionId }, (ev) => {
          if (ev.type === 'item' || ev.type === 'grading') {
            this.grading = { done: Number(ev.done) || 0, total: Number(ev.total) || 0 }
          } else if (ev.type === 'report') {
            this.report = ev.report as unknown as ExamReport
          } else if (ev.type === 'error') {
            toast(sseErrorText(ev, '阅卷没有完成，请稍后重试。'), 'error')
          }
        }, {
          onDone: async () => {
            this.grading = null
            this.busy = false
            this.exam = null
            this.answers = {}
            this.cursor = 0
            await this.openResult(sessionId)
            await this.load()
            resolve()
          },
          onError: () => {
            // 批到一半断了。**这一场没有废** —— 已批的题在库里，
            // 下次进页面 grading_pending 会把它捡回来接着批。
            this.grading = null
            this.busy = false
            toast('阅卷中断了，重新进入模考页会接着批完', 'error')
            resolve()
          },
        })
      })
    },

    /** 打开一份成绩单（刚考完的，或历史里的某一场）。 */
    async openResult(sessionId: number) {
      try {
        const full = await get<{ items: GradedItem[]; report: ExamReport }>(`/exam/result/${sessionId}`)
        this.reviewItems = full.items || []
        this.report = full.report
        this.resultId = sessionId
        this.persist(true)
      } catch {
        // 那场被删了、或不归自己：清掉这个 id，别每次进页面都去撞一次 404
        if (this.cache.resultId === sessionId) { this.resultId = 0; this.persist(true) }
      }
    },

    /** 关掉成绩单，回到开考前。 */
    closeResult() {
      this.report = null
      this.reviewItems = []
      this.resultId = 0
      this.persist(true)
    },

    async history(limit = 10): Promise<ExamHistoryRow[]> {
      const r = await get<{ items: ExamHistoryRow[] }>(`/exam/history?limit=${limit}`)
      return r?.items || []
    },

    async quit() {
      const exam = this.exam
      if (!exam) return
      if (!window.confirm('放弃这场模考？已作答的内容不会计分。')) return
      await post('/exam/abandon', { session_id: exam.session_id })
      this.stopTicker()
      this.exam = null
      this.answers = {}
      this.cursor = 0
      this.persist(true)
      await this.load()
    },

    /** 退出登录：停表、断流、清干净，别把上一个人的卷子留给下一个人。 */
    clearAll() {
      this.stopTicker()
      if (flushTimer) { clearTimeout(flushTimer); flushTimer = undefined }
      stopStream?.()
      stopStream = null
      this.exam = null
      this.preview = null
      this.answers = {}
      this.cursor = 0
      this.remaining = 0
      this.grading = null
      this.report = null
      this.reviewItems = []
      this.resultId = 0
      this.cache = emptyCache()
      snapshot.clear()
    },
  },
})
