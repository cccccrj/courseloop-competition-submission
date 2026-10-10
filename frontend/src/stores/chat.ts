import { defineStore } from 'pinia'
import { del, get } from '@/api/client'
import { postSSE, sseErrorText } from '@/api/sse'
import type { Citation, SSEEvent } from '@/api/types'
import { useSnapshot } from './snapshot'
import { cleanAnswer } from '@/utils/sanitize'

/**
 * 学习大厅的会话状态。
 *
 * 原先这些状态长在 Hall.vue 的组件内存里，只靠 MainLayout 的 keep-alive 撑着 ——
 * 切页确实不丢，但刷新、重新登录、换个标签页就全没了，而后端其实一直在往
 * learning_sessions / dialog_turns 里写。这个 store 把三件事分开：
 *
 *   1. **内存**：会话状态搬出组件，页面卸载（退出登录之外）都不影响它；
 *   2. **sessionStorage**：本标签页的快照，刷新后立刻能看到刚才那段对话，
 *      包括**还没写完就被打断的那半条回答** —— 那半条后端没落库，只有这里有；
 *   3. **后端**：/sessions 读回历史，换设备、清缓存之后仍然找得回来。
 *
 * 按科目分开存。原先切科目是 `messages = []`，等于"看一眼计组"就把数据结构
 * 那段问答烧了；现在四门各留各的，切回去还在。
 */

export interface ChatMsg {
  role: 'user' | 'assistant'
  content: string
  citations?: Citation[]
  verified?: boolean | null
  reasoning?: string
  thinking?: boolean
  /** 流被打断（刷新 / 断网 / 后端重启）后留下的半截回答，界面上要给出"重新生成"。 */
  interrupted?: boolean
  /** 已经请助教复核过这条回答（review_requests.id），避免重复提交。 */
  reviewRequestId?: number
}

export interface Conversation {
  /** learning_sessions.id；0 表示这段对话还没落库过。 */
  sessionId: number
  messages: ChatMsg[]
  /** 输入框里没发出去的草稿，刷新也不该丢。 */
  draft: string
  streaming: boolean
  /** 是否已经尝试过从后端读回历史，避免每次进页面都打一次接口。 */
  restored: boolean
}

export interface SessionBrief {
  id: number
  course_id: string
  intent: string
  title: string
  turns: number
  started_at: string
  last_at: string
}

const snapshot = useSnapshot<Record<string, Conversation>>('cl_chat_v1')

// 中断函数不进 state：它不可序列化，也不该被响应式代理包一层。
const aborts: Record<string, (() => void) | null> = {}

// 还没建过条目的科目读到的兜底。**只读** —— 所有写操作都走 ensure()。
const EMPTY: Conversation = Object.freeze(
  { sessionId: 0, messages: [], draft: '', streaming: false, restored: false }) as Conversation

function emptyConv(): Conversation {
  return { sessionId: 0, messages: [], draft: '', streaming: false, restored: false }
}

function loadCache(): Record<string, Conversation> {
  return snapshot.read((data) => {
    for (const conv of Object.values(data)) {
      // 快照是上一次页面加载留下的，那时的 fetch 早已随页面一起没了。
      // 不在这里落地的话，界面会显示一个永远转不完的「思考中」和一个点不动的停止键。
      if (conv.streaming) {
        conv.streaming = false
        const last = conv.messages[conv.messages.length - 1]
        if (last?.role === 'assistant') { last.thinking = false; last.interrupted = true }
      }
    }
  }) ?? {}
}

export const useChatStore = defineStore('chat', {
  state: () => ({ convs: loadCache() as Record<string, Conversation> }),

  getters: {
    // 只读。带参 getter 里写 state 会让这个 computed 自己失效、白跑一轮，
    // 所以建条目交给 ensure()，这里没有的就先给一份只读兜底。
    conv: (s) => (course: string): Conversation => s.convs[course] ?? EMPTY,
  },

  actions: {
    /** 取该门的会话，没有就建一条。所有写操作都从这里拿对象。 */
    ensure(course: string): Conversation {
      if (!this.convs[course]) this.convs[course] = emptyConv()
      return this.convs[course]
    },

    /** 写快照。节流与强制落盘的取舍见 stores/snapshot.ts。 */
    persist(immediate = false) {
      snapshot.write(() => this.convs, immediate)
    },

    /** 从后端读回该门最近一次对话。已有内容或已经读过就跳过。 */
    async restore(course: string) {
      const conv = this.ensure(course)
      if (conv.restored || conv.messages.length) { conv.restored = true; return }
      conv.restored = true
      try {
        const r = await get<{ items: SessionBrief[] }>(
          `/sessions?course=${encodeURIComponent(course)}&intent=chat&limit=1`)
        const last = r?.items?.[0]
        if (last) await this.open(course, last.id)
      } catch {
        /* 没登录 / 后端没起来：留空白页即可，不必打扰 */
      }
    },

    /** 打开指定的历史会话（用于历史列表）。 */
    async open(course: string, sessionId: number) {
      this.stop(course)
      const conv = this.ensure(course)
      const r = await get<{ turns: any[] }>(`/sessions/${sessionId}`)
      conv.sessionId = sessionId
      conv.messages = (r?.turns || []).map((t) => ({
        role: t.role === 'student' ? 'user' : 'assistant',
        // 修复前存进库里的老回答可能带着「回答完毕」这类标记（I-02）
        content: t.role === 'student' ? (t.content || '') : cleanAnswer(t.content),
        citations: t.citations || [],
        verified: t.verified ?? null,
      })) as ChatMsg[]
      conv.restored = true
      this.persist(true)
    },

    async history(course: string, limit = 20): Promise<SessionBrief[]> {
      const r = await get<{ items: SessionBrief[] }>(
        `/sessions?course=${encodeURIComponent(course)}&intent=chat&limit=${limit}`)
      return r?.items || []
    },

    async remove(course: string, sessionId: number) {
      await del(`/sessions/${sessionId}`)
      const conv = this.ensure(course)
      if (conv.sessionId === sessionId) this.reset(course)
    },

    /** 开一段新对话（当前这段已经在库里，不会丢）。 */
    reset(course: string) {
      this.stop(course)
      const conv = this.ensure(course)
      conv.sessionId = 0
      conv.messages = []
      conv.restored = true
      this.persist(true)
    },

    send(course: string, text: string) {
      const q = text.trim()
      const conv = this.ensure(course)
      if (!q || conv.streaming) return
      conv.draft = ''
      conv.messages.push({ role: 'user', content: q })
      conv.messages.push({ role: 'assistant', content: '', citations: [], verified: null,
                           reasoning: '', thinking: true })
      const bot = conv.messages[conv.messages.length - 1]
      conv.streaming = true
      this.persist(true)

      // 历史不含刚推进去的这两条（问句单独发，占位回答还是空的）
      const history = conv.messages.slice(0, -2).map((m) => ({ role: m.role, content: m.content }))
      const finish = () => {
        aborts[course] = null
        conv.streaming = false
        bot.thinking = false
        this.persist(true)
      }
      aborts[course] = postSSE(
        '/chat/stream',
        { message: q, history, course, session_id: conv.sessionId },
        (ev: SSEEvent) => {
          // 会话 id 由后端在第一帧下发，之后每一轮都带上它，整段对话才落在同一行。
          if (ev.type === 'session') { conv.sessionId = Number(ev.id) || 0; this.persist(true); return }
          if (ev.type === 'reasoning') bot.reasoning += String(ev.text ?? '')
          else if (ev.type === 'token' || ev.type === 'delta') {
            bot.thinking = false
            bot.content += String(ev.text ?? ev.content ?? '')
          } else if (ev.type === 'final') {
            // 流结束时后端发回清洗后的全文，替换掉逐 token 拼起来的那份
            bot.content = String(ev.text ?? bot.content)
          }
          else if (ev.type === 'citation') bot.citations!.push(ev as unknown as Citation)
          else if (ev.type === 'verify') bot.verified = Boolean(ev.passed)
          else if (ev.type === 'error') {
            bot.thinking = false
            bot.interrupted = true
            bot.content += `\n\n> ${sseErrorText(ev)}`
          }
          this.persist()
        },
        { onDone: finish, onError: () => { bot.interrupted = true; finish() } },
      )
    },

    /** 主动停止。半截回答留在界面上 —— 已经生成的内容对学生仍然有用。 */
    stop(course: string) {
      const cancel = aborts[course]
      aborts[course] = null
      cancel?.()
      const conv = this.ensure(course)
      conv.streaming = false
      const last = conv.messages[conv.messages.length - 1]
      if (last?.role === 'assistant') { last.thinking = false; if (!last.content) last.interrupted = true }
      this.persist(true)
    },

    /** 重发上一条问句（流被打断后用）。 */
    retry(course: string) {
      const conv = this.ensure(course)
      if (conv.streaming) return
      // 去掉那条打断的回答和它对应的问句，再按原样重发一次
      const last = conv.messages[conv.messages.length - 1]
      if (last?.role === 'assistant') conv.messages.pop()
      const ask = conv.messages.pop()
      if (!ask || ask.role !== 'user') return
      this.send(course, ask.content)
    },

    setDraft(course: string, text: string) {
      this.ensure(course).draft = text
      this.persist()
    },

    /** 退出登录：断掉所有流并清干净，别把上一个人的对话留给下一个人。 */
    clearAll() {
      for (const course of Object.keys(this.convs)) this.stop(course)
      this.convs = {}
      snapshot.clear()
    },
  },
})
