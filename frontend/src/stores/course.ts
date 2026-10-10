import { defineStore } from 'pinia'
import { get } from '../api/client'

const KEY = 'cl_course'

export interface CourseInfo {
  id: string
  display: string
  short: string
  exam: string
  score: number
  code_enabled: boolean
  verification_types: string[]
  topics: string[]
  sample_questions: string[]
}

// 兜底：后端 /courses 未就绪时也能展示四门（与 backend/core/courses.py 对齐）
const FALLBACK: CourseInfo[] = [
  { id: 'ds-algo', display: '《数据结构与算法》', short: '数据结构', exam: '408 统考', score: 45, code_enabled: true, verification_types: ['unit_test', 'complexity', 'rule', 'citation'], topics: [], sample_questions: [] },
  { id: 'co', display: '《计算机组成原理》', short: '计组', exam: '408 统考', score: 45, code_enabled: false, verification_types: ['numeric', 'rule', 'citation'], topics: [], sample_questions: [] },
  { id: 'os', display: '《操作系统》', short: '操作系统', exam: '408 统考', score: 35, code_enabled: false, verification_types: ['numeric', 'rule', 'citation'], topics: [], sample_questions: [] },
  { id: 'net', display: '《计算机网络》', short: '计算机网络', exam: '408 统考', score: 25, code_enabled: false, verification_types: ['numeric', 'rule', 'citation'], topics: [], sample_questions: [] },
]

export const useCourseStore = defineStore('course', {
  state: () => ({
    current: localStorage.getItem(KEY) || 'ds-algo',
    courses: [] as CourseInfo[],
    loaded: false,
  }),
  getters: {
    list: (s): CourseInfo[] => (s.courses.length ? s.courses : FALLBACK),
    info(): CourseInfo {
      return this.list.find((c) => c.id === this.current) || this.list[0]
    },
    codeEnabled(): boolean {
      return !!this.info?.code_enabled
    },
  },
  actions: {
    async load() {
      if (this.loaded) return
      try {
        const r = await get<{ items: CourseInfo[] }>('/courses')
        if (r?.items?.length) this.courses = r.items
      } catch {
        /* 兜底 FALLBACK */
      }
      this.loaded = true
    },
    set(id: string) {
      this.current = id
      localStorage.setItem(KEY, id)
    },
  },
})
