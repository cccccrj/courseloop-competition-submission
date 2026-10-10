/**
 * 模型输出上屏前的兜底清洗 —— 与 backend/core/sanitize.py 同一套规则，改一边记得改另一边。
 *
 * 后端在入库前已经清洗过；这里管两件后端管不到的事：
 * 1. 修复前就存进库里的老记录（测试报告 I-02：历史回答顶部的「---- 回答完毕 ----」）；
 * 2. 流式过程中，token 还在一个个到，最终清洗要等流结束才发回来。
 *
 * 只认**整行**都是标记的情况，正文里的「所有边处理完毕」和表格的 |----| 不受影响。
 */

const DECOR = /^[\s\-=*_—–~·.。:：!！【】[\]()（）<>《》#]+|[\s\-=*_—–~·.。:：!！【】[\]()（）<>《》#]+$/g
const MARKER =
  /^(?:以上|本次|本轮)?(?:回答|作答|输出|答案|证据|证据引用|引用|检索|处理|解析|思考|推理|生成|分析)(?:引用|过程|内容|部分|结果)?(?:处理)?(?:已)?(?:完毕|结束|完成|开始)$/
const EN_MARKER = /^(?:END|DONE|EOF|END OF ANSWER|ANSWER END)$/i
const RULE = /^\s*(?:[-=*_—–~]\s*){3,}$/
const FENCE = /^\s*(```|~~~)/
const PROMPT_ECHO = /evidence|source_id|JSON\s*对象|表达要求|系统提示|提示词|system prompt|STUDENT_STYLE/i

function isMarkerLine(line: string): boolean {
  const core = line.replace(DECOR, '')
  if (!core || core.length > 16) return false
  return MARKER.test(core) || EN_MARKER.test(core)
}

function filterLines(text: string, drop: (line: string) => boolean): string {
  const out: string[] = []
  let inCode = false
  for (const line of text.split('\n')) {
    if (FENCE.test(line)) { inCode = !inCode; out.push(line); continue }
    if (!inCode && drop(line)) continue
    out.push(line)
  }
  return out.join('\n')
}

function trimEdges(text: string): string {
  const lines = text.split('\n')
  while (lines.length && (!lines[0].trim() || RULE.test(lines[0]))) lines.shift()
  while (lines.length && (!lines[lines.length - 1].trim() || RULE.test(lines[lines.length - 1]))) lines.pop()
  return lines.join('\n')
}

export function stripMarkers(text: string | null | undefined): string {
  if (!text) return ''
  let t = text.replace(/<think>[\s\S]*?<\/think>/gi, '')
  if (/<\/think>/i.test(t)) t = t.replace(/^[\s\S]*?<\/think>/i, '')
  // 流式中途：<think> 已开、还没闭合，后面都是思考内容，先不上屏
  t = t.replace(/<think>[\s\S]*$/i, '')
  t = t.replace(/<\|[^|<>\n]{1,40}\|>/g, '')
  t = t.replace(/<\/?(?:answer|final_answer|final|response|output)>/gi, '')
  return trimEdges(filterLines(t, isMarkerLine))
}

export function cleanAnswer(text: string | null | undefined): string {
  return stripMarkers(text)
}

export function cleanReasoning(text: string | null | undefined): string {
  return trimEdges(filterLines(stripMarkers(text), (line) => PROMPT_ECHO.test(line)))
}
