/**
 * 周报分享卡：把一份周报画成一张 PNG。
 *
 * 为什么是手画 canvas，而不是截 DOM：
 * html2canvas / foreignObject 那一路要把页面 CSS 一起塞进 SVG，而本站的卡面靠
 * backdrop-filter、多层半透明渐变和 CSS 变量堆出来 —— 这些恰好是那条路径复现得
 * 最不可靠的部分，导出结果会随浏览器变，而且失败时是"图出来了但样子不对"，
 * 比报错还难发现。这里的每一笔都自己画，输出与页面无关，任何浏览器都是同一张图。
 *
 * 代价是这张卡与页面不共享样式，配色只能照抄 theme.css 的值（下面 C 常量）。
 * 改主题色时这里要跟着改一次 —— 用注释标出来，而不是假装它会自动同步。
 */

/** 抄自 theme.css。改那边时这里要同步。 */
const C = {
  bg: '#06080f',
  card: '#0f1424',
  border: '#232b42',
  brand: '#818cf8',
  brandDim: '#3730a3',
  accent: '#22d3ee',
  text: '#e9ecff',
  text2: '#aab3ce',
  text3: '#848eaf',
  ok: '#34d399',
  warn: '#fbbf24',
  err: '#f87171',
}

const W = 900
const PAD = 56
const FONT = "system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif"

function font(size: number, weight = 400) {
  return `${weight} ${size}px ${FONT}`
}

/** 按像素宽度折行。中文没有词边界，只能逐字量，所以不做贪心分词。 */
function wrap(ctx: CanvasRenderingContext2D, text: string, maxWidth: number): string[] {
  const lines: string[] = []
  let line = ''
  for (const ch of text) {
    if (ch === '\n') { lines.push(line); line = ''; continue }
    if (ctx.measureText(line + ch).width > maxWidth && line) {
      lines.push(line)
      line = ch
    } else {
      line += ch
    }
  }
  if (line) lines.push(line)
  return lines
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number,
                   w: number, h: number, r: number) {
  ctx.beginPath()
  ctx.moveTo(x + r, y)
  ctx.arcTo(x + w, y, x + w, y + h, r)
  ctx.arcTo(x + w, y + h, x, y + h, r)
  ctx.arcTo(x, y + h, x, y, r)
  ctx.arcTo(x, y, x + w, y, r)
  ctx.closePath()
}

export interface ShareCardInput {
  courseDisplay: string
  weekStart: string
  weekEnd: string
  who: string
  stats: { label: string; value: string }[]
  /** 掌握度变化。null = 没有可比基线，卡上要写"首次生成"而不是 0.0 */
  masteryDelta: number | null
  errorTypes: { name: string; cnt: number }[]
  weakPoints: { name: string; mastery: number }[]
  comment: string
}

/**
 * 画出分享卡，返回 PNG 的 dataURL。
 *
 * 高度是量出来的不是拍脑袋定的：先用一个离屏 ctx 量出点评要折几行，
 * 再决定画布多高。写死高度的话，点评长一点就被裁掉半句 —— 而被裁掉的
 * 恰好是"下周建议"那部分。
 */
export function drawShareCard(input: ShareCardInput, scale = 2): string {
  const probe = document.createElement('canvas').getContext('2d')!
  probe.font = font(19)
  const commentLines = input.comment ? wrap(probe, input.comment, W - PAD * 2 - 44) : []

  const headH = 168
  const statsH = 120
  const masteryH = 96
  const errH = input.errorTypes.length ? 52 + input.errorTypes.length * 34 : 0
  const weakH = input.weakPoints.length ? 52 + input.weakPoints.length * 34 : 0
  const commentH = commentLines.length ? 56 + commentLines.length * 30 : 0
  const footH = 78
  const H = headH + statsH + masteryH + errH + weakH + commentH + footH

  const canvas = document.createElement('canvas')
  canvas.width = W * scale
  canvas.height = H * scale
  const ctx = canvas.getContext('2d')!
  ctx.scale(scale, scale)
  ctx.textBaseline = 'top'

  // 底：纯色 + 左上角一团靛蓝光。全站的"用光不用色"在这张静态图上只剩这一处，
  // 但少了它整张卡会像一份报表而不像这个产品出的东西。
  ctx.fillStyle = C.bg
  ctx.fillRect(0, 0, W, H)
  const glow = ctx.createRadialGradient(120, 60, 0, 120, 60, 460)
  glow.addColorStop(0, 'rgba(99, 102, 241, .34)')
  glow.addColorStop(1, 'rgba(6, 8, 15, 0)')
  ctx.fillStyle = glow
  ctx.fillRect(0, 0, W, 440)

  let y = PAD

  // ---------- 抬头 ----------
  ctx.fillStyle = C.accent
  ctx.font = font(14, 800)
  ctx.fillText('COURSELOOP · 学情周报', PAD, y)
  y += 30
  ctx.fillStyle = C.text
  ctx.font = font(34, 800)
  ctx.fillText(`${input.who} 的本周小结`, PAD, y)
  y += 48
  ctx.fillStyle = C.text2
  ctx.font = font(18)
  ctx.fillText(`${input.courseDisplay} · ${input.weekStart} 至 ${input.weekEnd}`, PAD, y)
  y = headH

  // ---------- 四格数字 ----------
  const cols = Math.max(1, input.stats.length)
  const gap = 14
  const cw = (W - PAD * 2 - gap * (cols - 1)) / cols
  input.stats.forEach((s, i) => {
    const x = PAD + i * (cw + gap)
    ctx.fillStyle = C.card
    roundRect(ctx, x, y, cw, 92, 14)
    ctx.fill()
    ctx.strokeStyle = C.border
    ctx.lineWidth = 1
    ctx.stroke()
    ctx.fillStyle = i === 0 ? C.brand : C.text
    ctx.font = font(28, 800)
    ctx.fillText(s.value, x + 16, y + 18)
    ctx.fillStyle = C.text3
    ctx.font = font(14)
    ctx.fillText(s.label, x + 16, y + 56)
  })
  y += statsH

  // ---------- 掌握度变化 ----------
  ctx.fillStyle = C.card
  roundRect(ctx, PAD, y, W - PAD * 2, 72, 14)
  ctx.fill()
  ctx.strokeStyle = C.border
  ctx.stroke()
  ctx.fillStyle = C.text3
  ctx.font = font(15)
  ctx.fillText('本周平均掌握度变化', PAD + 18, y + 15)
  if (input.masteryDelta === null) {
    // 没有基线就说没有基线。写 +0.0% 会被读成"这周一点没涨"。
    ctx.fillStyle = C.text2
    ctx.font = font(20, 700)
    ctx.fillText('首次生成，暂无上周基线', PAD + 18, y + 38)
  } else {
    const d = input.masteryDelta
    ctx.fillStyle = d > 0 ? C.ok : d < 0 ? C.err : C.text2
    ctx.font = font(26, 800)
    ctx.fillText(`${d > 0 ? '+' : ''}${(d * 100).toFixed(1)}%`, PAD + 18, y + 34)
  }
  y += masteryH

  // ---------- 错因分布 ----------
  if (input.errorTypes.length) {
    ctx.fillStyle = C.text
    ctx.font = font(18, 700)
    ctx.fillText('本周错因分布', PAD, y)
    y += 34
    const max = Math.max(...input.errorTypes.map((t) => t.cnt))
    const barX = PAD + 168
    const barMax = W - PAD - barX - 48
    for (const t of input.errorTypes) {
      ctx.fillStyle = C.text2
      ctx.font = font(16)
      ctx.fillText(t.name, PAD, y + 3)
      ctx.fillStyle = C.brandDim
      roundRect(ctx, barX, y + 4, barMax, 16, 8)
      ctx.fill()
      ctx.fillStyle = C.brand
      roundRect(ctx, barX, y + 4, Math.max(8, (barMax * t.cnt) / max), 16, 8)
      ctx.fill()
      ctx.fillStyle = C.text3
      ctx.font = font(15)
      ctx.fillText(String(t.cnt), barX + barMax + 14, y + 4)
      y += 34
    }
    y += 18
  }

  // ---------- 下周该补的 ----------
  if (input.weakPoints.length) {
    ctx.fillStyle = C.text
    ctx.font = font(18, 700)
    ctx.fillText('下周优先补这几个', PAD, y)
    y += 34
    for (const p of input.weakPoints) {
      ctx.fillStyle = C.warn
      ctx.beginPath()
      ctx.arc(PAD + 5, y + 11, 4, 0, Math.PI * 2)
      ctx.fill()
      ctx.fillStyle = C.text2
      ctx.font = font(16)
      ctx.fillText(p.name, PAD + 20, y + 3)
      ctx.fillStyle = C.text3
      ctx.font = font(15)
      const label = `掌握度 ${Math.round(p.mastery * 100)}%`
      ctx.fillText(label, W - PAD - ctx.measureText(label).width, y + 3)
      y += 34
    }
    y += 18
  }

  // ---------- 点评 ----------
  if (commentLines.length) {
    ctx.fillStyle = C.card
    roundRect(ctx, PAD, y, W - PAD * 2, commentLines.length * 30 + 34, 14)
    ctx.fill()
    ctx.strokeStyle = C.border
    ctx.stroke()
    // 左边一道亮条，标明这段是模型写的，与上面的确定性数字区分开
    ctx.fillStyle = C.accent
    roundRect(ctx, PAD, y + 12, 3, commentLines.length * 30 + 10, 2)
    ctx.fill()
    ctx.fillStyle = C.text2
    ctx.font = font(19)
    commentLines.forEach((line, i) => ctx.fillText(line, PAD + 22, y + 20 + i * 30))
    y += commentLines.length * 30 + 34 + 22
  }

  // ---------- 落款 ----------
  ctx.fillStyle = C.text3
  ctx.font = font(14)
  ctx.fillText('数据来自本人真实作答与判题记录 · CourseLoop', PAD, H - PAD + 8)

  return canvas.toDataURL('image/png')
}

export function downloadDataUrl(dataUrl: string, filename: string) {
  const a = document.createElement('a')
  a.href = dataUrl
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
}

export interface ErrorCardInput {
  courseDisplay: string
  who: string
  knowledge: string
  errorName: string
  stem: string
  diagnosis: string
  createdAt: string
  resolved: boolean
}

/**
 * 错题分享卡。与周报卡共用同一套画笔（C / W / PAD / wrap / roundRect），
 * 所以两张图放在一起是同一个产品出的。
 *
 * 刻意不画正确答案：这张卡是拿去问同学、发到群里的，带上答案就没得讨论了。
 * 画的是"题目 + 我错在哪"，那恰好也是最值得被别人看见的部分。
 */
export function drawErrorCard(input: ErrorCardInput, scale = 2): string {
  const probe = document.createElement('canvas').getContext('2d')!
  probe.font = font(20)
  const stemLines = wrap(probe, input.stem || '（未记录题干）', W - PAD * 2 - 44).slice(0, 8)
  probe.font = font(18)
  const diagLines = wrap(probe, input.diagnosis || '', W - PAD * 2 - 44).slice(0, 10)

  const headH = 150
  const stemH = 40 + stemLines.length * 32 + 24
  const diagH = diagLines.length ? 46 + diagLines.length * 30 + 24 : 0
  const footH = 78
  const H = headH + stemH + diagH + footH

  const canvas = document.createElement('canvas')
  canvas.width = W * scale
  canvas.height = H * scale
  const ctx = canvas.getContext('2d')!
  ctx.scale(scale, scale)
  ctx.textBaseline = 'top'

  ctx.fillStyle = C.bg
  ctx.fillRect(0, 0, W, H)
  const glow = ctx.createRadialGradient(120, 60, 0, 120, 60, 460)
  // 错题卡用琥珀光而不是靛蓝：答错是学习过程的一步，用警示色标记，
  // 但不用红 —— 与判题结果块里 .bad 的选色同一条理由。
  glow.addColorStop(0, 'rgba(251, 191, 36, .26)')
  glow.addColorStop(1, 'rgba(6, 8, 15, 0)')
  ctx.fillStyle = glow
  ctx.fillRect(0, 0, W, 420)

  let y = PAD

  ctx.fillStyle = C.warn
  ctx.font = font(15, 700)
  ctx.fillText('错题卡 · ' + input.courseDisplay, PAD, y)
  y += 30
  ctx.fillStyle = C.text
  ctx.font = font(34, 800)
  ctx.fillText(input.knowledge || '未归类知识点', PAD, y)
  y += 48

  // 错因徽章 + 状态
  const tag = input.errorName || '未标注错因'
  ctx.font = font(15, 700)
  const tagW = ctx.measureText(tag).width + 26
  ctx.fillStyle = 'rgba(251, 191, 36, .16)'
  roundRect(ctx, PAD, y, tagW, 30, 15)
  ctx.fill()
  ctx.fillStyle = C.warn
  ctx.fillText(tag, PAD + 13, y + 7)
  const state = input.resolved ? '已订正' : '待订正'
  const stateW = ctx.measureText(state).width + 26
  ctx.fillStyle = input.resolved ? 'rgba(52, 211, 153, .16)' : 'rgba(148, 163, 220, .12)'
  roundRect(ctx, PAD + tagW + 10, y, stateW, 30, 15)
  ctx.fill()
  ctx.fillStyle = input.resolved ? C.ok : C.text3
  ctx.fillText(state, PAD + tagW + 23, y + 7)
  y += 52

  // 题干
  ctx.fillStyle = C.card
  roundRect(ctx, PAD, y, W - PAD * 2, stemLines.length * 32 + 28, 14)
  ctx.fill()
  ctx.strokeStyle = C.border
  ctx.stroke()
  ctx.fillStyle = C.text
  ctx.font = font(20)
  stemLines.forEach((line, i) => ctx.fillText(line, PAD + 22, y + 14 + i * 32))
  y += stemLines.length * 32 + 28 + 24

  // 诊断：左侧亮条，与周报卡的"AI 点评"同一标记语言
  if (diagLines.length) {
    ctx.fillStyle = C.card
    roundRect(ctx, PAD, y, W - PAD * 2, diagLines.length * 30 + 30, 14)
    ctx.fill()
    ctx.strokeStyle = C.border
    ctx.stroke()
    ctx.fillStyle = C.accent
    roundRect(ctx, PAD, y + 12, 3, diagLines.length * 30 + 6, 2)
    ctx.fill()
    ctx.fillStyle = C.text2
    ctx.font = font(18)
    diagLines.forEach((line, i) => ctx.fillText(line, PAD + 22, y + 15 + i * 30))
    y += diagLines.length * 30 + 30 + 24
  }

  ctx.fillStyle = C.text3
  ctx.font = font(14)
  ctx.fillText(`${input.who} · ${input.createdAt} · CourseLoop 错题本`, PAD, H - PAD + 8)

  return canvas.toDataURL('image/png')
}
