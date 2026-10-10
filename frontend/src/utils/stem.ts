/**
 * 选择题题干里连写在一行的选项拆成每项一行。
 *
 * 题库里相当一部分选择题（尤其是生成题与早期导入的题）把选项和题干写在同一行：
 * 「……哪一项是正确的？ A. 栈是先进先出 B. 插入只能在栈底 C. …… D. ……」。
 * 页面上读起来要来回找字母（第二阶段视觉走查）。只在 A、B、C 依次出现、且还没分行时才拆，
 * 已经分好行的题干原样返回；只认 A–D 后面紧跟「. ． 、」的写法，不碰正文里的「B 树」这类字样。
 */
const OPT = /(^|[\s\u3000：:？?。，,])([A-D])[.．、]\s*/g

export function splitChoiceOptions(stem: string | null | undefined): string {
  const s = stem || ''
  if (!s) return s
  const hits = [...s.matchAll(OPT)]
  const seq = hits.map((m) => m[2]).join('')
  if (!/A.*B.*C/.test(seq)) return s
  // 每个字母后面都得真有选项内容（≥ 2 个字）才算选项：
  // 「设有 A、B、C 三个进程」里字母挨着字母，不是选项，不拆
  for (let i = 0; i < hits.length; i++) {
    const start = hits[i].index! + hits[i][0].length
    const end = i + 1 < hits.length ? hits[i + 1].index! : s.length
    if (s.slice(start, end).trim().length < 2) return s
  }
  if (/\n[ \t]*B[.．、]/.test(s)) return s          // 已经一项一行
  // 字母前若是标点（「……的：A．」），标点留在题干那一行；是空白就直接换成换行
  return s.replace(OPT, (_m, pre: string, letter: string) =>
    `${/\s/.test(pre) || pre === '' ? '' : pre}${pre === '' ? '' : '\n'}${letter}. `).replace(/^\n/, '')
}
