/** 通用院系示例。选择器允许用户创建未列出的条目。 */
export interface CollegeGroup {
  label: string
  options: string[]
}

export const COLLEGE_GROUPS: CollegeGroup[] = [
  {
    label: '院系示例',
    options: [
      '信息与计算学院',
      '工程学院',
      '理学院',
      '管理学院',
      '人文学院',
    ],
  },
]
