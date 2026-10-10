import { defineStore } from 'pinia'
import { post } from '@/api/client'
import { postSSE, sseErrorText } from '@/api/sse'
import type { Citation } from '@/api/types'
import { useSnapshot } from './snapshot'

/**
 * 智能解题与纠错的会话状态。
 *
 * 与大厅同一套思路（见 stores/chat.ts）：状态搬出组件、快照进 sessionStorage、
 * 历史从后端读回。解题这边多一层麻烦 —— TutorGraph 的状态机原先只活在后端
 * **进程内存**里，后端一重启，学生手里的 session_id 就变成"会话不存在"，
 * 题还在屏幕上、提交按钮却已经废了。现在后端每步都存快照（tutor_snapshots），
 * 这里用 /tutor/resume 把题目、对话流、提示级别一起接回来。
 */

export interface SolveTurn {
  role: 'student' | 'tutor'
  kind?: 'ask' | 'diagnosis' | 'hint' | 'correct' | 'variant' | 'think' | 'error'
  text?: string
  streaming?: boolean
  data?: any
}

export interface SolveSession {
  /** TutorGraph 会话 id（uuid）。空串表示还没开始解题。 */
  sessionId: string
  /** learning_sessions.id，用于读回。 */
  dbSessionId: number
  problem: string
  /** 切到变式题之后，原题留在这里（空串 = 当前就是原题）。 */
  originProblem?: string
  /** 作答框里的草稿。 */
  answer: string
  turns: SolveTurn[]
  busy: boolean
  mastery: number | null
  masteryState: string
  resolved: boolean
  citations: Citation[]
  restored: boolean
}

const snapshot = useSnapshot<Record<string, SolveSession>>('cl_solve_v1')

const aborts: Record<string, (() => void) | null> = {}

// 还没建过条目的科目读到的兜底。**只读** —— 所有写操作都走 ensure()。
const EMPTY: SolveSession = Object.freeze(
  { sessionId: '', dbSessionId: 0, problem: '', answer: '', turns: [], busy: false,
    mastery: null, masteryState: '', resolved: false, citations: [], restored: false }) as SolveSession

function empty(): SolveSession {
  return { sessionId: '', dbSessionId: 0, problem: '', answer: '', turns: [], busy: false,
           mastery: null, masteryState: '', resolved: false, citations: [], restored: false }
}

function loadCache(): Record<string, SolveSession> {
  return snapshot.read((data) => {
    for (const s of Object.values(data)) {
      // 上一次页面加载留下的"进行中"，那个请求早已不在了
      s.busy = false
      for (const t of s.turns) if (t.streaming) t.streaming = false
    }
  }) ?? {}
}

export const useSolveStore = defineStore('solve', {
  state: () => ({ sessions: loadCache() as Record<string, SolveSession> }),

  getters: {
    // 只读，理由同 stores/chat.ts：建条目走 ensure()。
    session: (s) => (course: string): SolveSession => s.sessions[course] ?? EMPTY,
  },

  actions: {
    /** 取该门的解题会话，没有就建一条。 */
    ensure(course: string): SolveSession {
      if (!this.sessions[course]) this.sessions[course] = empty()
      return this.sessions[course]
    },

    /** 写快照。节流与强制落盘的取舍见 stores/snapshot.ts。 */
    persist(immediate = false) {
      snapshot.write(() => this.sessions, immediate)
    },

    /** 读回该门最近一次没做完的解题会话。 */
    async restore(course: string) {
      const s = this.ensure(course)
      if (s.restored || s.sessionId) { s.restored = true; return }
      s.restored = true
      try {
        const r: any = await post('/tutor/resume', { course })
        if (!r?.session_id) return
        s.sessionId = r.session_id
        s.dbSessionId = r.db_session_id || 0
        s.problem = r.problem || ''
        s.originProblem = r.origin_problem || ''
        s.turns = r.turns || []
        s.mastery = typeof r.mastery === 'number' ? r.mastery : null
        s.resolved = Boolean(r.resolved)
        this.persist(true)
      } catch {
        /* 没登录 / 没有历史：空白起步即可 */
      }
    },

    async start(course: string, problem: string) {
      const s = this.ensure(course)
      const p = problem.trim()
      if (!p || s.busy) return
      s.busy = true
      try {
        const r: any = await post('/tutor/start', { problem: p, course })
        s.sessionId = r.session_id
        s.dbSessionId = r.db_session_id || 0
        s.problem = p
        s.originProblem = ''
        s.citations = r.citations || []
        s.turns = [{ role: 'tutor', kind: 'ask', text: r.message }]
        s.resolved = false
        s.mastery = null
        s.answer = ''
        this.persist(true)
      } finally {
        s.busy = false
      }
    },

    submit(course: string) {
      const s = this.ensure(course)
      const a = s.answer.trim()
      if (!a || s.busy || !s.sessionId) return
      s.busy = true
      s.turns.push({ role: 'student', text: a })
      s.answer = ''
      s.turns.push({ role: 'tutor', kind: 'think', text: '', streaming: true })
      const think = s.turns[s.turns.length - 1]
      this.persist(true)

      const finish = () => {
        aborts[course] = null
        think.streaming = false
        s.busy = false
        this.persist(true)
      }
      aborts[course] = postSSE(
        '/tutor/submit/stream',
        { session_id: s.sessionId, student_answer: a },
        (ev: any) => {
          if (ev.type === 'reasoning') { think.text += ev.text; this.persist() }
          else if (ev.type === 'result') {
            think.streaming = false
            s.mastery = ev.mastery ?? s.mastery
            s.masteryState = ev.mastery_state || s.masteryState
            if (ev.correct) {
              s.resolved = true
              s.turns.push({ role: 'tutor', kind: 'correct', text: ev.message })
            } else {
              s.turns.push({ role: 'tutor', kind: 'diagnosis', data: ev })
            }
            this.persist(true)
          } else if (ev.type === 'error') {
            think.streaming = false
            // 此前这里用的是 kind 'correct'，界面会先给一个「作答正确」徽标再说「出错了」
            s.turns.push({ role: 'tutor', kind: 'error', text: sseErrorText(ev) })
            this.persist(true)
          }
        },
        { onDone: finish, onError: finish },
      )
    },

    async hint(course: string) {
      const s = this.ensure(course)
      if (s.busy || !s.sessionId) return
      s.busy = true
      try {
        const r: any = await post('/tutor/hint', { session_id: s.sessionId })
        if (r?.hint) s.turns.push({ role: 'tutor', kind: 'hint', data: r.hint })
        this.persist(true)
      } finally {
        s.busy = false
      }
    },

    async variant(course: string) {
      const s = this.ensure(course)
      if (s.busy || !s.sessionId) return
      s.busy = true
      try {
        const r: any = await post('/tutor/variant', { session_id: s.sessionId })
        if (r?.error) {
          s.turns.push({ role: 'tutor', kind: 'error', text: String(r.error) })
        } else if (r?.problem) {
          s.turns.push({ role: 'tutor', kind: 'variant', data: r.problem })
          // 后端已把会话切到这道变式题：接下来的作答按变式题判分（docs/34 §3.2）
          if (r.current_problem) s.problem = r.current_problem
          s.originProblem = r.origin_problem || s.originProblem || ''
          s.resolved = false
          s.answer = ''
        }
        this.persist(true)
      } finally {
        s.busy = false
      }
    },

    setAnswer(course: string, text: string) {
      this.ensure(course).answer = text
      this.persist()
    },

    /** 换一题：断流清空。这次的作答、掌握度、错题早已落库，丢的只是屏幕上的会话。 */
    reset(course: string) {
      const cancel = aborts[course]
      aborts[course] = null
      cancel?.()
      this.sessions[course] = { ...empty(), restored: true }
      this.persist(true)
    },

    clearAll() {
      for (const course of Object.keys(this.sessions)) {
        const cancel = aborts[course]
        aborts[course] = null
        cancel?.()
      }
      this.sessions = {}
      snapshot.clear()
    },
  },
})
