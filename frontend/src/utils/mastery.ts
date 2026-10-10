/**
 * 平均掌握度的展示 —— 学生首页、错题与学习记录、助教学生详情三处共用（测试报告 I-03）。
 *
 * 数字本身由后端 profile.summarize_mastery 统一算（练过的知识点、带遗忘衰减），
 * 这里只管"怎么写出来"：平均值后面总跟着覆盖面，免得一个百分比被读成"整门课学会了多少"。
 */
export interface MasterySummary {
  average: number | null
  practiced: number
  total: number
  mastered: number
}

/** 没练过任何知识点时显示"—"，不显示 0%：0% 读起来像"学了但全不会"。 */
export function masteryPct(avg: number | null | undefined): string {
  return avg === null || avg === undefined ? '—' : `${Math.round(avg * 100)}%`
}

export function coverageText(s: Pick<MasterySummary, 'practiced' | 'total'> | null | undefined): string {
  return s ? `已练 ${s.practiced}/${s.total} 个知识点` : ''
}
