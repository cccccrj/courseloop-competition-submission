<script setup lang="ts">
/**
 * 知识图谱学习地图：把 /profile/weak 已经返回的那份知识点画像换一种渲染方式。
 *
 * 三点需要先说清楚，否则这页很容易被误读：
 *
 * 1) **边是大纲层级，不是先修依赖图。** 课程 → 章 → 知识点是 408 大纲本来的两层结构；
 *    章与章之间那条淡线是**教学顺序**。这个项目里没有 knowledge_prerequisites 表，
 *    408 大纲本身也不含先修信息（见 profile.py:_mark_prereq 的说明）。
 *    凭空画一张先修图会让人以为系统知道"学 A 必须先学 B"，那是假的。
 *
 * 2) **颜色编码的是量级（掌握度），所以用单色相亮度梯度，不用彩虹。**
 *    深色底下低掌握=暗靛、高掌握=亮靛。"未练过"不是色阶的最低档 ——
 *    它是"没有数据"，走中性灰 + 虚线环，不然会被读成"掌握度为 0"，
 *    而这两件事对学生意味着完全不同的下一步。
 *
 * 3) **概念错误用保留状态色，且不靠颜色单独承载**：红环 + 卫星点 + 图例 + 悬浮里写字。
 *
 * 数据一律取 /profile/weak（练习页与复习计划共用的同一份排序），不另开取数口 ——
 * 否则又会出现"图谱说先补树、练习页一直推排序"这类没法解释的不一致。
 */
import MarkdownView from '@/components/MarkdownView.vue'
import { computed, onActivated, onDeactivated, onUnmounted, ref, shallowRef, watch, nextTick } from 'vue'
import { useThemeStore } from '@/stores/theme'
import { useRouter } from 'vue-router'
import { get } from '@/api/client'
import { useCourseStore } from '@/stores/course'
import CLPageHeader from '@/components/ui/CLPageHeader.vue'
import CLBadge from '@/components/ui/CLBadge.vue'
import CLButton from '@/components/ui/CLButton.vue'
import CLProgress from '@/components/ui/CLProgress.vue'
import CLSegmented from '@/components/ui/CLSegmented.vue'
import CLSkeleton from '@/components/ui/CLSkeleton.vue'
import CLEmpty from '@/components/ui/CLEmpty.vue'

const router = useRouter()
const courseStore = useCourseStore()
const themeStore = useThemeStore()

/* ==================== 取数 ==================== */
interface KP {
  knowledge_id: number; name: string; chapter: string; chapter_index: number
  mastery: number; mastery_raw: number; attempts: number; correct_n: number
  error_freq: number; error_total: number; top_error_name: string
  has_misconception: boolean; bank_count: number; days_since: number | null
  state: string; priority: number; target_difficulty: number
}

const items = shallowRef<KP[]>([])
const errors = shallowRef<any[]>([])
const loading = ref(true)
const coldStart = ref(false)
// 取数失败和「这门课确实没有知识点」是两回事，页面要说的话也不一样。
// 原先没有这个标记：请求一挂，items 停在空数组，页面就去说「还没有知识点数据」，
// 把一次后端故障说成了学生自己的数据状态，而且给的下一步（切一门课）没有用。
const failed = ref(false)

async function load() {
  loading.value = true
  failed.value = false
  try {
    const c = encodeURIComponent(courseStore.current)
    // limit=0 才是全量：默认 10 只够排薄弱榜，画图得把整门课的知识点都拿到
    // 两个请求互不依赖，并行取（原先是一个等完再发另一个）
    const [p, e] = await Promise.all([
      get<{ items: KP[]; cold_start: boolean }>(`/profile/weak?course=${c}&limit=0`),
      get<{ items: any[] }>(`/records/errors?course=${c}`),
    ])
    items.value = p.items || []
    coldStart.value = !!p.cold_start
    errors.value = e.items || []
  } catch {
    // 具体原因由全局拦截器弹出，这里只负责让页面主体不是一片空白
    failed.value = true
    items.value = []
    errors.value = []
  } finally {
    loading.value = false
  }
  build()
}

onActivated(async () => { readPalette(); await courseStore.load(); await load() })
watch(() => courseStore.current, () => { selected.value = null; load() })

const STATE_LABEL: Record<string, string> = {
  not_started: '未开始', learning: '学习中', fragile: '不稳固',
  mastered: '已掌握', needs_review: '待复习', misconception: '概念错误',
}

/* ==================== 色标 ==================== */
// 掌握度 = 量级 → 单色相（靛蓝）五档亮度梯度。三档主题各有一组取值，
// 定义在 theme.css 的 --cl-map-ramp-1..5，夜间档是暗→亮、浅色两档是亮→暗。
// 每组都要满足：亮度单调、相邻 ΔL ≥ .06、两端对各自卡面可辨。换色请连着重校验。
//
// 这里必须走 JS 读 CSS 变量，而不是像图谱的描边那样直接写在 <style> 里：
// 节点填充是 :fill 绑定到 SVG 属性上的，属性值不解析 var()。
const RAMP_LABEL = ['0–20%', '20–40%', '40–60%', '60–80%', '80–100%']
// 兜底 = 夜间档的原值。只有在 CSS 尚未应用时才会用到（理论上不会发生，
// 但读不到变量时返回空串会让整张图变透明，代价太大）。
const RAMP_FALLBACK = ['#4338ca', '#5b5ef0', '#818cf8', '#a5b4fc', '#e0e7ff']
const NEUTRAL_FALLBACK = '#5c6893'

const RAMP = ref<string[]>([...RAMP_FALLBACK])
// "没有数据"不是色阶的一档：中性色 + 虚线环，与靛蓝色阶一眼分得开
const NEUTRAL = ref(NEUTRAL_FALLBACK)

function cssVar(name: string, fallback: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return v || fallback
}
function readPalette() {
  RAMP.value = RAMP_FALLBACK.map((fb, i) => cssVar(`--cl-map-ramp-${i + 1}`, fb))
  NEUTRAL.value = cssVar('--cl-map-neutral', NEUTRAL_FALLBACK)
}
// 换肤时重读。这是全站唯一一处"CSS 变量喂给 JS"的链路，切档后如果节点颜色
// 没跟着变，先查这个 watch。
watch(() => themeStore.mode, readPalette)

function rampOf(m: number): string {
  const r = RAMP.value
  return r[Math.min(r.length - 1, Math.max(0, Math.floor(m * r.length)))]
}

/* ==================== 建图 ==================== */
type Kind = 'root' | 'chapter' | 'topic'
interface GNode {
  id: string; kind: Kind; label: string
  x: number; y: number; vx: number; vy: number; r: number
  fx: number | null; fy: number | null
  charge: number
  chapter: string
  touched: boolean
  mastery: number
  misconception: boolean
  kp: KP | null
  members: number          // 章节节点下挂的知识点数
}
interface GEdge { a: GNode; b: GNode; len: number; k: number; seq: boolean }

const nodes = shallowRef<GNode[]>([])
const edges = shallowRef<GEdge[]>([])
// 力导向每帧都在改坐标。节点对象刻意保持非响应式（shallowRef 不深转换），
// 靠 frame 这一个计数器触发重渲染 —— 让 Vue 逐个追踪 141 个节点的 x/y 会白烧一半帧时间。
const frame = ref(0)

// 坐标系 = 画布的实际像素尺寸（ResizeObserver 量出来）。原先写死 1180×760，而画布实际约 750×560，
// SVG 先被整体缩到 63%，取景再缩一次，字最后只剩 4px 左右（用户反馈"显示得很小"）。
const VB = ref({ w: 800, h: 560 })
const CHAPTER_R = 236          // 章节环半径
const TOPIC_R = 66             // 知识点绕各自章节的半径

function build() {
  const list = items.value
  if (!list.length) { nodes.value = []; edges.value = []; frame.value++; return }

  const byChapter = new Map<string, KP[]>()
  for (const it of list) {
    const key = it.chapter || '其他知识点'
    if (!byChapter.has(key)) byChapter.set(key, [])
    byChapter.get(key)!.push(it)
  }
  // 章节按大纲顺序排（chapter_index），而不是按薄弱程度 —— 图的骨架应当稳定：
  // 学生练了几道题回来，节点位置整体重排会让人以为图变了内容。
  const chapters = [...byChapter.entries()].sort((a, b) => {
    const ia = Math.min(...a[1].map((x) => x.chapter_index))
    const ib = Math.min(...b[1].map((x) => x.chapter_index))
    return ia - ib || a[0].localeCompare(b[0])
  })

  const ns: GNode[] = []
  const es: GEdge[] = []
  const mk = (o: Partial<GNode> & { id: string; kind: Kind; label: string }): GNode => {
    const n: GNode = {
      x: 0, y: 0, vx: 0, vy: 0, r: 7, fx: null, fy: null, charge: -240,
      chapter: '', touched: false, mastery: 0, misconception: false, kp: null, members: 0,
      ...o,
    } as GNode
    ns.push(n)
    return n
  }

  const root = mk({
    id: 'root', kind: 'root', label: courseStore.info?.short || '课程',
    r: 26, charge: -2600, fx: 0, fy: 0,
  })

  chapters.forEach(([name, kps], i) => {
    const ang = (i / chapters.length) * Math.PI * 2 - Math.PI / 2
    const touched = kps.filter((k) => k.attempts || k.error_total)
    const avg = touched.length
      ? touched.reduce((s, k) => s + (k.mastery || 0), 0) / touched.length : 0
    const ch = mk({
      id: `ch:${name}`, kind: 'chapter', label: name, chapter: name,
      x: Math.cos(ang) * CHAPTER_R, y: Math.sin(ang) * CHAPTER_R,
      r: 13, charge: -1150, mastery: avg, touched: !!touched.length, members: kps.length,
    })
    es.push({ a: root, b: ch, len: CHAPTER_R, k: 0.42, seq: false })

    kps.forEach((kp, j) => {
      // 同章知识点按大纲内顺序铺在章节外侧的扇形上，扇口朝背离圆心的方向
      const spread = Math.min(Math.PI * 1.5, 0.5 + kps.length * 0.22)
      const a2 = ang + (kps.length === 1 ? 0 : (j / (kps.length - 1) - 0.5) * spread)
      mk({
        id: `kp:${kp.knowledge_id}`, kind: 'topic', label: kp.name, chapter: name,
        x: Math.cos(ang) * CHAPTER_R + Math.cos(a2) * TOPIC_R,
        y: Math.sin(ang) * CHAPTER_R + Math.sin(a2) * TOPIC_R,
        r: 7, charge: -300,
        touched: !!(kp.attempts || kp.error_total), mastery: kp.mastery || 0,
        misconception: !!kp.has_misconception || kp.state === 'misconception', kp,
      })
      es.push({ a: ch, b: ns[ns.length - 1], len: TOPIC_R, k: 0.5, seq: false })
    })
  })

  // 章与章之间的顺序线：只表示教学顺序，权重刻意压得很低，
  // 它的作用是把相邻章拉近成"路径"，不是一条真实的依赖边。
  const chNodes = ns.filter((n) => n.kind === 'chapter')
  for (let i = 0; i + 1 < chNodes.length; i++) {
    es.push({ a: chNodes[i], b: chNodes[i + 1], len: 210, k: 0.05, seq: true })
  }

  nodes.value = ns
  edges.value = es
  view.value = { x: 0, y: 0, k: 1 }
  // 一次算到收敛再画。原先从种子位置逐帧"长开"，节点出现后还要再动 4 秒多才停（实测 4.2s），
  // 这期间取景一直在缩放 —— 看起来就是"加载慢"。40 个节点跑到收敛约 240 步、不到几十毫秒。
  // 拖拽节点时的弹性（reheat）照旧逐帧动画，那是交互反馈，不是加载。
  stopLoop()
  alpha = 1
  for (let i = 0; i < 400 && alpha > 0.004; i++) tick()
  alpha = 0
  autofit = true
  settle()
}

/** 取景 + 标签避让，直到取景不再变。
 *
 * 标签按屏幕像素画之后，力导向按"点"排出来的间距装不下字，会叠在一起。
 * 取景定了（k 已知）再按标签的实际占位推开重叠，推完重新取景，几轮就稳定。
 * 画布尺寸一变（首次挂载量到真实大小、窗口缩放）就要重来一遍：画布变小 → k 变小 →
 * 同样的字在图坐标里占得更宽，原来不叠的又会叠上。 */
function settle() {
  fitView()
  for (let round = 0; round < 5; round++) {
    const k0 = view.value.k
    relaxLabels(k0)
    fitView()
    if (Math.abs(view.value.k - k0) / k0 < 0.01) break
  }
  frame.value++
}

/** 标签避让：每个节点连同它的标签看成一个矩形（屏幕像素 ÷ k 换回图坐标），
 *  两两重叠就沿重叠较少的那一轴各退一半。根节点固定在中心不动。 */
function relaxLabels(k: number) {
  const ns = nodes.value
  const box = ns.map((n) => {
    const fs = n.kind === 'topic' ? 11.5 : n.kind === 'chapter' ? 13 : 14
    // 中文按一字一个字号宽算（偏保守，宁可松一点也不要叠）
    const hw = (Math.max(n.r, n.label.length * fs / 2) + 3) / k
    // 矩形盖住"圆点 + 标签"的真实上下沿（与模板里 text 的 y 一致）：
    // 知识点标签基线在 r+12（字在点下方），章节 / 根标签基线在 -(r+8)（字在点上方）
    const top = n.kind === 'topic' ? -n.r : -(n.r + 8) - fs
    const bottom = n.kind === 'topic' ? n.r + 14 : n.r
    return { hw, hh: (bottom - top) / 2 / k + 1 / k, oy: (top + bottom) / 2 / k }
  })
  for (let it = 0; it < 200; it++) {
    let moved = false
    for (let i = 0; i < ns.length; i++) {
      for (let j = i + 1; j < ns.length; j++) {
        const a = ns[i], b = ns[j], ba = box[i], bb = box[j]
        const dx = b.x - a.x
        const dy = (b.y + bb.oy) - (a.y + ba.oy)
        const ox = ba.hw + bb.hw - Math.abs(dx)
        const oy = ba.hh + bb.hh - Math.abs(dy)
        if (ox <= 0 || oy <= 0) continue
        moved = true
        const aFixed = a.kind === 'root', bFixed = b.kind === 'root'
        const wa = aFixed ? 0 : bFixed ? 1 : 0.5
        const wb = bFixed ? 0 : aFixed ? 1 : 0.5
        if (ox < oy) {
          const push = (ox + 0.5) * Math.sign(dx || (i - j))
          a.x -= push * wa; b.x += push * wb
        } else {
          const push = (oy + 0.5) * Math.sign(dy || (i - j))
          a.y -= push * wa; b.y += push * wb
        }
      }
    }
    if (!moved) break
  }
}

/* ==================== 力导向 ==================== */
// 自己写的极简版 d3-force（连线弹簧 + 斥力 + 向心力）：141 个节点 O(n²) 每帧
// 约两万次运算，比引入一个绘图依赖划算，也省得为深色主题去覆写别人的默认样式。
let alpha = 0
let raf = 0
let lastAt = 0

function tick() {
  const ns = nodes.value
  const es = edges.value
  // 连线弹簧
  for (const e of es) {
    let dx = e.b.x - e.a.x, dy = e.b.y - e.a.y
    let d = Math.hypot(dx, dy) || 0.001
    const f = ((d - e.len) / d) * alpha * e.k
    e.b.vx -= dx * f * 0.5; e.b.vy -= dy * f * 0.5
    e.a.vx += dx * f * 0.5; e.a.vy += dy * f * 0.5
  }
  // 斥力
  for (let i = 0; i < ns.length; i++) {
    const a = ns[i]
    for (let j = i + 1; j < ns.length; j++) {
      const b = ns[j]
      let dx = b.x - a.x, dy = b.y - a.y
      let d2 = dx * dx + dy * dy
      if (d2 < 1) { dx = (i - j) * 0.5 || 0.5; dy = 0.5; d2 = 1 }
      const w = alpha / d2
      a.vx += dx * a.charge * w; a.vy += dy * a.charge * w
      b.vx -= dx * b.charge * w; b.vy -= dy * b.charge * w
    }
  }
  // 向心力 + 积分
  for (const n of ns) {
    if (n.fx !== null) { n.x = n.fx; n.y = n.fy!; n.vx = 0; n.vy = 0; continue }
    n.vx -= n.x * 0.022 * alpha
    n.vy -= n.y * 0.022 * alpha
    n.vx *= 0.62; n.vy *= 0.62
    n.x += n.vx; n.y += n.vy
  }
  alpha *= 0.977
}

function loop(now: number) {
  // 步数按**流逝时间**补，不按帧数：低端机或后台标签页里 rAF 会掉到个位数 Hz，
  // 按帧计步的话同一张图在快机器上 1 秒铺开、在慢机器上要抖十几秒。
  // 上限 20 步是防呆：切回一个挂了很久的标签页时 dt 可能是几十秒，
  // 不封顶就会在一帧里把整段模拟跑完，图"啪"地跳到终态。
  const steps = Math.min(20, Math.max(1, Math.round((now - lastAt) / 16.7)))
  lastAt = now
  for (let i = 0; i < steps && alpha > 0.004; i++) tick()
  // 每帧都跟着取景，而不是等收敛完再框一次：图是从种子位置向外"长开"的，
  // 中途会一度撑出画布 —— 只在最后框一次，用户看到的第一秒就是被裁掉的图，
  // 而这一秒恰好是这页唯一的第一印象。多跑一趟包围盒的开销可以忽略。
  if (autofit) fitView()
  frame.value++
  if (alpha > 0.004) { raf = requestAnimationFrame(loop); return }
  raf = 0
  // 拖拽后的弹性回位停下来时，同样把挤在一起的标签推开
  relaxLabels(view.value.k)
  frame.value++
}

/** 收敛后按实际包围盒缩放到刚好铺满画布。
 *
 * 不能靠调常数解决：四门课的章节数是 5–7、知识点 23–34，同一套半径参数下
 * 图的实际尺寸差出小一半，写死 viewBox 必然有一门课要么撑破要么缩成一小团。 */
let autofit = false
const K_MIN = 0.25
const K_MAX = 3
function fitView() {
  const ns = nodes.value
  if (!ns.length) return
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity
  for (const n of ns) {
    x0 = Math.min(x0, n.x); x1 = Math.max(x1, n.x)
    y0 = Math.min(y0, n.y); y1 = Math.max(y1, n.y)
  }
  // 节点和标签按**屏幕像素**画（见节点上的 scale(1/k)），不随缩放变小，
  // 所以边距也按像素留：左右留一个最长标签的半宽，上下各留一行字。
  const longest = Math.max(...ns.map((n) => n.label.length))
  const padX = Math.min(110, longest * 6.5 + 16)
  const padY = 36
  const { w, h } = VB.value
  const k = Math.min(K_MAX, Math.max(K_MIN,
    Math.min((w - 2 * padX) / Math.max(1, x1 - x0), (h - 2 * padY) / Math.max(1, y1 - y0))))
  view.value = { k, x: -((x0 + x1) / 2) * k, y: -((y0 + y1) / 2) * k }
  frame.value++
}

function reheat(a = 0.4) {
  alpha = Math.max(alpha, a)
  if (!raf) { lastAt = performance.now(); raf = requestAnimationFrame(loop) }
}

// 这页被 keep-alive 缓存，切走时走 deactivated 而不是 unmounted。
// 不停 rAF 的话，人在别的页面上做题，这里还在后台空转烧帧。
function stopLoop() { if (raf) { cancelAnimationFrame(raf); raf = 0 } }
onDeactivated(stopLoop)
onUnmounted(stopLoop)

/* ==================== 视图变换（缩放 / 平移 / 拖拽） ==================== */
const svgEl = ref<SVGSVGElement | null>(null)
// 量画布实际尺寸，坐标系跟着它（见上方 VB 的说明）
let ro: ResizeObserver | null = null
watch(() => svgEl.value, (el) => {
  ro?.disconnect()
  if (!el) return
  ro = new ResizeObserver(([entry]) => {
    const { width, height } = entry.contentRect
    if (width > 0 && height > 0) {
      VB.value = { w: width, h: height }
      if (autofit) settle()
    }
  })
  ro.observe(el)
}, { flush: 'post' })
onUnmounted(() => ro?.disconnect())
const view = ref({ x: 0, y: 0, k: 1 })
const transform = computed(() => `translate(${view.value.x} ${view.value.y}) scale(${view.value.k})`)
// 节点的反向缩放：位置跟着缩放走，圆点和文字保持屏幕上的固定大小（"语义缩放"）。
// 缩小看全局时字不会糊成一片，放大看局部时点也不会大得离谱。
const inv = computed(() => 1 / view.value.k)

function toLocal(ev: { clientX: number; clientY: number }) {
  const svg = svgEl.value
  if (!svg) return { x: 0, y: 0 }
  const m = svg.getScreenCTM()
  if (!m) return { x: 0, y: 0 }
  const p = new DOMPoint(ev.clientX, ev.clientY).matrixTransform(m.inverse())
  return { x: (p.x - view.value.x) / view.value.k, y: (p.y - view.value.y) / view.value.k }
}

function onWheel(ev: WheelEvent) {
  autofit = false          // 用户一动手就交还取景权，否则自动归位会把他刚缩放的视野抢走
  const before = toLocal(ev)
  const k = Math.min(K_MAX, Math.max(K_MIN, view.value.k * (ev.deltaY < 0 ? 1.12 : 1 / 1.12)))
  view.value.k = k
  const after = toLocal(ev)
  // 以指针为锚点缩放：不补这两下平移，缩放会把内容甩出视野
  view.value.x += (after.x - before.x) * k
  view.value.y += (after.y - before.y) * k
}

let drag: { node: GNode | null; ox: number; oy: number } | null = null

function onPointerDown(ev: PointerEvent, node: GNode | null) {
  autofit = false
  ;(ev.currentTarget as Element).setPointerCapture?.(ev.pointerId)
  const p = toLocal(ev)
  if (node) {
    node.fx = node.x; node.fy = node.y
    drag = { node, ox: p.x - node.x, oy: p.y - node.y }
    reheat(0.32)
  } else {
    drag = { node: null, ox: ev.clientX - view.value.x, oy: ev.clientY - view.value.y }
  }
}

function onPointerMove(ev: PointerEvent) {
  if (!drag) return
  if (drag.node) {
    const p = toLocal(ev)
    drag.node.fx = p.x - drag.ox
    drag.node.fy = p.y - drag.oy
    reheat(0.32)
  } else {
    view.value.x = ev.clientX - drag.ox
    view.value.y = ev.clientY - drag.oy
  }
}

function onPointerUp() {
  // 松手就放开约束：节点重新参与受力，图会自己合拢，比钉死原地更像"活的"
  if (drag?.node) { drag.node.fx = null; drag.node.fy = null; reheat(0.22) }
  drag = null
}

function resetView() { autofit = true; settle() }

/* ==================== 悬浮与选中 ==================== */
const hover = shallowRef<GNode | null>(null)
const tip = ref({ x: 0, y: 0, show: false })
const selected = shallowRef<GNode | null>(null)

function onNodeEnter(ev: PointerEvent, n: GNode) {
  hover.value = n
  const box = (svgEl.value?.parentElement as HTMLElement)?.getBoundingClientRect()
  if (box) tip.value = { x: ev.clientX - box.left, y: ev.clientY - box.top, show: true }
}
function onNodeMove(ev: PointerEvent) {
  if (!hover.value) return
  const box = (svgEl.value?.parentElement as HTMLElement)?.getBoundingClientRect()
  if (box) tip.value = { x: ev.clientX - box.left, y: ev.clientY - box.top, show: true }
}
function onNodeLeave() { hover.value = null; tip.value.show = false }

function pick(n: GNode) {
  if (n.kind === 'root') { selected.value = null; return }
  selected.value = selected.value?.id === n.id ? null : n
}

/* ==================== 过滤 / 搜索 / 视图模式 ==================== */
const mode = ref('graph')
const MODES = [{ label: '图谱', value: 'graph' }, { label: '表格', value: 'table' }]
const query = ref('')
const weakOnly = ref(false)
// 默认全标签：单门课只有 23–34 个知识点（116 是四门合计），一屏放得下。
// 只在悬浮时才给名字，图就退化成一堆没有语义的点 —— 好看，但读不出任何东西。
const allLabels = ref(true)

const matched = computed(() => {
  const q = query.value.trim().toLowerCase()
  const ids = new Set<string>()
  if (!q && !weakOnly.value) return null      // null = 不过滤，全部正常显示
  for (const n of nodes.value) {
    if (n.kind === 'root') continue
    const hitQ = !q || n.label.toLowerCase().includes(q) || n.chapter.toLowerCase().includes(q)
    const hitW = !weakOnly.value || (n.kind === 'topic'
      ? (n.kp!.mastery < 0.6 || n.kp!.error_freq > 0)
      : true)
    if (hitQ && hitW) ids.add(n.id)
  }
  return ids
})

// 过滤只改"是否淡出"，不改配色 —— 颜色跟的是掌握度这个绝对量，
// 换个筛选条件就重新上色的话，两次截图之间同一个知识点会变颜色。
function dimmed(n: GNode): boolean {
  if (n.kind === 'root') return false
  const m = matched.value
  if (m && !m.has(n.id)) return true
  const s = selected.value
  if (!s) return false
  return !(n.id === s.id || n.chapter === s.chapter)
}

function labelVisible(n: GNode): boolean {
  if (n.kind !== 'topic') return true
  if (allLabels.value) return true
  if (selected.value && (n.id === selected.value.id || n.chapter === selected.value.chapter)) return true
  if (hover.value?.id === n.id) return true
  return !!(matched.value && matched.value.has(n.id) && query.value.trim())
}

/* ==================== 概览与下钻 ==================== */
const summary = computed(() => {
  const l = items.value
  const touched = l.filter((k) => k.attempts || k.error_total)
  const mastered = l.filter((k) => k.mastery >= 0.8)
  const misc = l.filter((k) => k.has_misconception || k.state === 'misconception')
  return { total: l.length, touched: touched.length, mastered: mastered.length, misc: misc.length }
})

const selKp = computed<KP | null>(() => selected.value?.kp || null)
const selErrors = computed(() =>
  selKp.value ? errors.value.filter((e) => e.knowledge_id === selKp.value!.knowledge_id) : [])
const selProblems = shallowRef<any[]>([])
const selLoading = ref(false)

watch(selKp, async (kp) => {
  selProblems.value = []
  if (!kp) return
  selLoading.value = true
  try {
    const r = await get<{ items: any[] }>(
      `/problems?course=${encodeURIComponent(courseStore.current)}&knowledge=${encodeURIComponent(kp.name)}`)
    selProblems.value = r.items || []
  } catch {
    /* 题库拿不到时面板照常展示画像，不把整块打空 */
  } finally {
    selLoading.value = false
  }
})

function practiceThis() {
  if (!selKp.value) return
  router.push({ path: '/practice', query: { course: courseStore.current, kid: selKp.value.knowledge_id } })
}

// 表格视图与图谱同源同序（profile 的 priority 降序），换个呈现而已
const tableRows = computed(() => {
  const m = matched.value
  return items.value.filter((k) => !m || m.has(`kp:${k.knowledge_id}`))
})

function focusRow(k: KP) {
  const n = nodes.value.find((x) => x.id === `kp:${k.knowledge_id}`)
  if (!n) return
  mode.value = 'graph'
  selected.value = n
  nextTick(() => fitView())
}
</script>

<template>
  <div>
    <CLPageHeader title="知识图谱学习地图"
      sub="颜色越亮掌握越好；点一个知识点看它的错题和可练题目" />

    <!-- 筛选控件一行，压在图上面：每个视图各挂一套筛选，两边的数就会开始不一致 -->
    <div class="toolbar">
      <CLSegmented v-model="mode" :options="MODES" />
      <label class="search">
        <input v-model="query" type="text" placeholder="搜知识点或章节，如「树」「进程」" />
      </label>
      <label class="chk"><input v-model="weakOnly" type="checkbox" /><span>只看薄弱</span></label>
      <label v-if="mode === 'graph'" class="chk">
        <input v-model="allLabels" type="checkbox" /><span>显示全部标签</span>
      </label>
      <span class="spacer"></span>
      <CLButton v-if="mode === 'graph'" variant="ghost" @click="resetView">重置视图</CLButton>
    </div>

    <!-- 原先是四张大统计卡，把图压到了首屏 380px 以下；收成一行，把首屏让给图 -->
    <p v-if="!loading && items.length" class="stats-line">
      共 <b>{{ summary.total }}</b> 个知识点 · 已练过 <b>{{ summary.touched }}</b> ·
      已掌握（≥80%）<b>{{ summary.mastered }}</b> · 存在概念错误 <b :class="{ bad: summary.misc }">{{ summary.misc }}</b>
    </p>

    <CLSkeleton v-if="loading" :rows="6" />

    <CLEmpty v-else-if="failed" title="知识图谱没能加载出来"
      hint="可能是网络或服务暂时不可用。稍等一下再试，或者先去做几道练习。">
      <CLButton @click="load">重试</CLButton>
    </CLEmpty>

    <CLEmpty v-else-if="!items.length" title="这门课还没有知识点数据"
      hint="换一门课看看；这门课的知识点大纲可能还在配置中。" />

    <div v-else class="layout">
      <!-- ============ 图谱 / 表格 ============ -->
      <section class="cl-card canvas-card" :class="{ table: mode === 'table' }">
        <p v-if="coldStart" class="cold">
          还没有作答记录，图上的知识点都是灰色。练几道题，颜色就会亮起来。
        </p>

        <template v-if="mode === 'graph'">
          <div class="canvas">
            <!-- SVG 本身对读屏只给一句概述：逐节点的可达路径由「表格」视图承担，
                 141 个节点做成 141 个 Tab 停靠点对键盘用户是灾难而不是无障碍。 -->
            <svg ref="svgEl" class="svg" :viewBox="`${-VB.w / 2} ${-VB.h / 2} ${VB.w} ${VB.h}`"
              role="img"
              :aria-label="`${courseStore.info?.short} 知识图谱：共 ${summary.total} 个知识点，其中 ${summary.mastered} 个已掌握；逐条数据见表格视图`"
              @wheel.prevent="onWheel"
              @pointerdown="onPointerDown($event, null)"
              @pointermove="onPointerMove"
              @pointerup="onPointerUp"
              @pointerleave="onPointerUp">
              <defs>
                <!-- stop-color 走 CSS（见 .km-core-in / .km-core-out）而不是属性：
                     属性值不解析 var()，写死就不跟主题 -->
                <radialGradient id="km-core">
                  <stop class="km-core-in" offset="0%" />
                  <stop class="km-core-out" offset="100%" />
                </radialGradient>
                <filter id="km-glow" x="-80%" y="-80%" width="260%" height="260%">
                  <feGaussianBlur stdDeviation="5" result="b" />
                  <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
                </filter>
              </defs>

              <!-- data-frame 不是装饰：节点对象是非响应式的（见 nodes 的说明），
                   模板里没有任何一处会因为 x/y 变化而重渲染。把 frame 读进渲染函数，
                   力导向每跑一帧就靠它触发一次重绘。删掉它整张图会静止不动。 -->
              <g :transform="transform" :data-frame="frame">
                <!-- 边先画，节点压在上面 -->
                <g class="edges">
                  <line v-for="(e, i) in edges" :key="i"
                    :class="['edge', { seq: e.seq, dim: dimmed(e.a) && dimmed(e.b) }]"
                    :x1="e.a.x" :y1="e.a.y" :x2="e.b.x" :y2="e.b.y" />
                </g>

                <!-- 节点的 pointermove 刻意不加 .stop：拖节点时指针一直压在节点上，
                     拦下这个事件就传不到 svg 的拖拽处理上，节点会拖不动。 -->
                <g class="nodes">
                  <g v-for="n in nodes" :key="n.id"
                    :class="['node', n.kind, { dim: dimmed(n), on: selected?.id === n.id }]"
                    :transform="`translate(${n.x} ${n.y}) scale(${inv})`"
                    @pointerdown.stop="onPointerDown($event, n)"
                    @pointerenter="onNodeEnter($event, n)"
                    @pointermove="onNodeMove"
                    @pointerleave="onNodeLeave"
                    @click.stop="pick(n)">
                    <!-- 命中区大于图形：7px 的点谁也点不准 -->
                    <circle class="hit" :r="Math.max(n.r + 10, 15)" />
                    <circle v-if="selected?.id === n.id" class="ring-sel" :r="n.r + 6" />
                    <circle class="dot" :r="n.r"
                      :fill="n.kind === 'root' ? 'url(#km-core)' : (n.touched ? rampOf(n.mastery) : NEUTRAL)"
                      :class="{ untouched: n.kind !== 'root' && !n.touched }"
                      :filter="n.kind === 'root' ? 'url(#km-glow)' : undefined" />
                    <!-- 概念错误：环 + 卫星点 + 图例 + 悬浮文字，不让颜色单独扛语义 -->
                    <template v-if="n.misconception">
                      <circle class="ring-misc" :r="n.r + 3.5" />
                      <circle class="sat-misc" :cx="n.r + 2" :cy="-(n.r + 2)" r="2.6" />
                    </template>
                    <text v-if="labelVisible(n)" class="label"
                      :y="n.kind === 'topic' ? n.r + 12 : -(n.r + 8)">{{ n.label }}</text>
                  </g>
                </g>
              </g>
            </svg>

            <!-- 悬浮读数：数值在前、名字在后 —— 手已经指在节点上了，要的是数 -->
            <div v-if="tip.show && hover" class="tip"
              :style="{ left: `${tip.x}px`, top: `${tip.y}px` }">
              <div class="tip-name">{{ hover.label }}</div>
              <template v-if="hover.kind === 'topic' && hover.kp">
                <div class="tip-row">
                  <b>{{ hover.touched ? `${Math.round(hover.mastery * 100)}%` : '未练过' }}</b>
                  <span>掌握度</span>
                </div>
                <div class="tip-row"><b>{{ hover.kp.attempts }}</b><span>次作答</span></div>
                <div class="tip-row"><b>{{ hover.kp.error_freq }}</b><span>条未解决错题</span></div>
                <div class="tip-row"><b>{{ hover.kp.bank_count }}</b><span>道可练题目</span></div>
                <div class="tip-tags">
                  <span class="tip-state">{{ STATE_LABEL[hover.kp.state] || hover.kp.state }}</span>
                  <span v-if="hover.misconception" class="tip-misc">概念错误</span>
                </div>
              </template>
              <template v-else-if="hover.kind === 'chapter'">
                <div class="tip-row"><b>{{ hover.members }}</b><span>个知识点</span></div>
                <div class="tip-row">
                  <b>{{ hover.touched ? `${Math.round(hover.mastery * 100)}%` : '未练过' }}</b>
                  <span>本章均值（仅统计练过的）</span>
                </div>
              </template>
              <template v-else>
                <div class="tip-row"><b>{{ summary.total }}</b><span>个知识点</span></div>
              </template>
            </div>
          </div>

          <!-- 图例常驻：颜色是唯一的量级通道，没有图例就没法读 -->
          <div class="legend">
            <span class="lg-title">掌握度</span>
            <span v-for="(c, i) in RAMP" :key="c" class="lg-step">
              <i :style="{ background: c }"></i>{{ RAMP_LABEL[i] }}
            </span>
            <span class="lg-sep"></span>
            <span class="lg-step"><i class="lg-untouched" :style="{ background: NEUTRAL }"></i>未练过（无数据）</span>
            <span class="lg-step"><i class="lg-misc"></i>概念错误</span>
          </div>
          <p class="axis-note">
            连线是<b>大纲层级</b>（课程 → 章 → 知识点），章与章之间的淡线只表示<b>教学顺序</b>；
            408 大纲不含先修信息，这里不画先修依赖。滚轮缩放、拖背景平移、拖节点可拨开重叠。
          </p>
        </template>

        <!-- 表格视图：图谱的无障碍对照，取的是同一份数据同一个排序 -->
        <template v-else>
          <div class="table-wrap">
            <table class="kt">
              <thead>
                <tr>
                  <th>知识点</th><th>章节</th><th class="num">掌握度</th><th>状态</th>
                  <th class="num">作答</th><th class="num">未解决错题</th><th class="num">题库</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="k in tableRows" :key="k.knowledge_id" @click="focusRow(k)">
                  <td class="kp-name">{{ k.name }}</td>
                  <td class="muted">{{ k.chapter || '—' }}</td>
                  <td class="num">
                    <span class="mini">
                      <i :style="{ width: `${Math.round((k.mastery || 0) * 100)}%`,
                                   background: (k.attempts || k.error_total) ? rampOf(k.mastery) : NEUTRAL }"></i>
                    </span>
                    {{ (k.attempts || k.error_total) ? `${Math.round((k.mastery || 0) * 100)}%` : '—' }}
                  </td>
                  <td>
                    <CLBadge :variant="k.has_misconception ? 'err' : k.mastery >= 0.8 ? 'ok' : 'neutral'">
                      {{ STATE_LABEL[k.state] || k.state }}
                    </CLBadge>
                  </td>
                  <td class="num">{{ k.attempts }}</td>
                  <td class="num" :class="{ warn: k.error_freq > 0 }">{{ k.error_freq }}</td>
                  <td class="num muted">{{ k.bank_count }}</td>
                </tr>
              </tbody>
            </table>
            <p v-if="!tableRows.length" class="axis-note">没有符合筛选条件的知识点。</p>
          </div>
        </template>
      </section>

      <!-- ============ 下钻面板 ============ -->
      <aside class="side">
        <div v-if="!selKp" class="cl-card side-empty">
          <CLEmpty title="点一个知识点"
            hint="图上任意节点都能下钻：看它的掌握度、未解决错题和可练题目" />
        </div>

        <template v-else>
          <div class="cl-card block">
            <div class="s-head">
              <div>
                <div class="s-name">{{ selKp.name }}</div>
                <div class="s-chapter">{{ selKp.chapter || '未归章' }}</div>
              </div>
              <CLBadge :variant="selKp.has_misconception ? 'err' : selKp.mastery >= 0.8 ? 'ok' : 'neutral'">
                {{ STATE_LABEL[selKp.state] || selKp.state }}
              </CLBadge>
            </div>
            <CLProgress :value="Math.round((selKp.mastery || 0) * 100)" label="掌握度"
              :right="`${Math.round((selKp.mastery || 0) * 100)}%`"
              :tone="selKp.has_misconception ? 'err' : selKp.mastery >= 0.8 ? 'brand' : 'warn'" />
            <div class="s-grid">
              <div><b>{{ selKp.attempts }}</b><span>作答次数</span></div>
              <div><b>{{ selKp.correct_n }}</b><span>答对</span></div>
              <div><b>{{ selKp.error_freq }}</b><span>未解决错题</span></div>
              <div><b>{{ selKp.bank_count }}</b><span>题库题量</span></div>
              <div><b>{{ selKp.days_since === null ? '—' : `${selKp.days_since}` }}</b><span>天前练过</span></div>
              <div><b>{{ selKp.target_difficulty }}</b><span>建议难度</span></div>
            </div>
            <p v-if="selKp.top_error_name" class="s-err">
              最常见错因：<b>{{ selKp.top_error_name }}</b>
            </p>
            <CLButton @click="practiceThis">去练这个知识点</CLButton>
          </div>

          <div class="cl-card block">
            <div class="b-head"><b>这个点的错题</b><CLBadge>{{ selErrors.length }} 条</CLBadge></div>
            <CLEmpty v-if="!selErrors.length" title="还没有错题" hint="这个知识点上暂时没有留下错题" />
            <ul v-else class="mini-list">
              <li v-for="e in selErrors.slice(0, 8)" :key="e.id" :class="{ done: e.resolved }">
                <div class="ml-top">
                  <CLBadge :variant="e.resolved ? 'ok' : 'warn'">{{ e.resolved ? '已解决' : '未解决' }}</CLBadge>
                  <span class="ml-tag">{{ e.error_name }}</span>
                </div>
                <div class="ml-text"><MarkdownView :source="e.stem || e.diagnosis || ''" /></div>
              </li>
            </ul>
          </div>

          <div class="cl-card block">
            <div class="b-head"><b>可练题目</b><CLBadge>{{ selProblems.length }} 道</CLBadge></div>
            <CLSkeleton v-if="selLoading" :card="false" :rows="3" />
            <CLEmpty v-else-if="!selProblems.length" title="题库还没有这个点的题"
              hint="这个知识点的练习题正在补充中，可以先看上面的相关错题。" />
            <ul v-else class="mini-list">
              <li v-for="p in selProblems.slice(0, 8)" :key="p.id">
                <div class="ml-top">
                  <CLBadge variant="cite">难度 {{ p.difficulty }}</CLBadge>
                  <span class="ml-tag">{{ p.verification_type }}</span>
                </div>
                <div class="ml-text">{{ p.stem }}</div>
              </li>
            </ul>
          </div>
        </template>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
.spacer { flex: 1 1 auto; }
/* 输入槽比卡面更暗，聚焦时提一档 —— 与 CLField 同一套材质 */
.search input {
  width: 260px; padding: 8px 12px; font-size: 13px;
  color: var(--cl-text); background: var(--cl-inset);
  border: 1px solid var(--cl-row-border); border-radius: 10px; outline: none;
  transition: border-color .18s ease, background .18s ease, box-shadow .18s ease;
}
.search input::placeholder { color: var(--cl-text-3); }
.search input:focus { background: var(--cl-inset-focus); border-color: var(--cl-brand); box-shadow: var(--cl-focus); }
.chk { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--cl-text-2); cursor: pointer; user-select: none; }
.chk input { accent-color: var(--cl-brand); }

.stats-line { margin: 0 0 12px; font-size: 13px; color: var(--cl-text-3); }
.stats-line b { color: var(--cl-text); font-size: 14px; margin: 0 1px; }
.stats-line b.bad { color: var(--cl-err); }

.layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 340px); gap: 18px; align-items: start; }
@media (max-width: 1180px) { .layout { grid-template-columns: minmax(0, 1fr); } }

.canvas-card { display: flex; flex-direction: column; gap: 12px; padding: 14px 16px 16px; }
.cold {
  margin: 0; padding: 10px 14px; border-radius: 10px;
  background: var(--cl-row); border: 1px solid var(--cl-row-border);
  border-left: 3px solid var(--cl-warn); box-shadow: 0 0 18px -10px var(--cl-warn);
  font-size: 12.5px; color: var(--cl-text-2);
}

/* 画布与卡面拉开一档，成为"另一种介质"：图是被观察的对象，不该跟卡面平齐。
   夜间是压暗，浅色两档是压出一点冷/暖底 —— 方向相反，语义相同。 */
.canvas {
  position: relative; border-radius: 12px; overflow: hidden;
  background: var(--cl-map-canvas);
  border: 1px solid var(--cl-row-border);
}
/* 根节点的径向渐变。stop 只能用 stop-color，不能用 background */
.km-core-in { stop-color: var(--cl-map-ramp-5); }
.km-core-out { stop-color: var(--cl-map-ramp-2); }
/* 高度跟着窗口走：写死 560 时，大屏上图只占半屏，小屏上又要滚动才看得全 */
.svg { display: block; width: 100%; height: clamp(480px, calc(100vh - 290px), 860px); touch-action: none; cursor: grab; }
.svg:active { cursor: grabbing; }
@media (max-width: 720px) { .svg { height: 420px; } }

/* 边是背景信息：压到刚好能看出结构，不能跟节点抢 */
.edge { stroke: var(--cl-map-edge); stroke-width: 1.1; transition: opacity .2s ease; vector-effect: non-scaling-stroke; }
.edge.seq { stroke: var(--cl-tint); stroke-dasharray: 3 6; }
.edge.dim { opacity: .28; }

.node { cursor: pointer; }
.node .hit { fill: transparent; }
.node .dot { transition: opacity .2s ease; }
/* 未练过：中性色之外再给一道虚线环，颜色之外还有一层形状线索 */
.node .dot.untouched { stroke: var(--cl-map-node-stroke); stroke-width: 1; stroke-dasharray: 2.5 2.5; }
.node.chapter .dot { stroke: var(--cl-map-node-stroke); stroke-width: 1.4; }
.node.dim { opacity: .2; }
.node:hover .dot { stroke: var(--cl-map-node-hover); stroke-width: 2; }
.ring-sel { fill: none; stroke: var(--cl-accent); stroke-width: 2; opacity: .9; }
.ring-misc { fill: none; stroke: var(--cl-err); stroke-width: 1.6; opacity: .85; }
.sat-misc { fill: var(--cl-err); }

/* 文字一律走文本色，不穿节点的颜色 —— 颜色在这页是数据通道，不是装饰 */
.label {
  text-anchor: middle; fill: var(--cl-text-2); font-size: 11.5px;
  paint-order: stroke; stroke: var(--cl-map-label-halo); stroke-width: 3px; stroke-linejoin: round;
  pointer-events: none;
}
.node.chapter .label { fill: var(--cl-text); font-size: 13px; font-weight: 700; }
.node.root .label { fill: var(--cl-text); font-size: 14px; font-weight: 700; }

.tip {
  position: absolute; z-index: 5; pointer-events: none;
  transform: translate(14px, -50%);
  min-width: 168px; padding: 10px 12px; border-radius: 10px;
  background: var(--cl-popover); border: 1px solid var(--cl-glass-border);
  box-shadow: var(--cl-shadow-md);
}
.tip-name { font-size: 12.5px; font-weight: 700; color: var(--cl-text); margin-bottom: 6px; }
.tip-row { display: flex; align-items: baseline; gap: 6px; font-size: 12px; color: var(--cl-text-3); }
.tip-row b { font-size: 14px; color: var(--cl-text); font-variant-numeric: tabular-nums; }
.tip-tags { display: flex; gap: 6px; margin-top: 6px; }
.tip-state { font-size: 11px; color: var(--cl-brand-600); }
.tip-misc { font-size: 11px; color: var(--cl-err-fg); }

.legend { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; font-size: 11.5px; color: var(--cl-text-3); }
.lg-title { font-weight: 700; color: var(--cl-text-2); }
.lg-step { display: inline-flex; align-items: center; gap: 5px; }
.lg-step i { width: 12px; height: 12px; border-radius: 3px; display: inline-block; }
.lg-untouched { border: 1px dashed var(--cl-map-node-stroke); }
.lg-misc { border: 1.6px solid var(--cl-err); border-radius: 999px !important; background: transparent; }
.lg-sep { width: 1px; height: 14px; background: var(--cl-row-border); }
.axis-note { margin: 0; font-size: 12px; line-height: 1.7; color: var(--cl-text-3); }
.axis-note b { color: var(--cl-text-2); font-weight: 600; }

/* ---------- 表格 ---------- */
.canvas-card.table { padding-bottom: 8px; }
.table-wrap { max-height: 640px; overflow: auto; }
.kt { width: 100%; border-collapse: collapse; font-size: 13px; }
.kt th {
  position: sticky; top: 0; z-index: 1; text-align: left; font-weight: 700; font-size: 12px;
  /* 吸顶表头必须不透明：玻璃卡面下用半透明色，滚动时行会从表头底下透出来 */
  color: var(--cl-text-3); padding: 8px 10px; background: var(--cl-surface);
  border-bottom: 1px solid var(--cl-row-border);
}
.kt td { padding: 9px 10px; border-bottom: 1px solid var(--cl-row-border); color: var(--cl-text-2); }
.kt tbody tr { cursor: pointer; }
.kt tbody tr:hover { background: var(--cl-row-hover); }
.kt .num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.kt .num.warn { color: var(--cl-warn-fg); }
.kt .muted { color: var(--cl-text-3); }
.kp-name { color: var(--cl-text); font-weight: 600; }
.mini { display: inline-block; width: 46px; height: 5px; border-radius: 999px; background: var(--cl-inset-deep); vertical-align: middle; margin-right: 8px; overflow: hidden; }
.mini i { display: block; height: 100%; border-radius: 999px; }

/* ---------- 侧栏 ---------- */
.side { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.side-empty { padding: 8px; }
.block { display: flex; flex-direction: column; gap: 12px; }
.s-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.s-name { font-size: 15px; font-weight: 700; color: var(--cl-text); }
.s-chapter { font-size: 12px; color: var(--cl-text-3); margin-top: 2px; }
.s-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
/* 行只靠"比卡面亮一点"分层，不再套一层玻璃 */
.s-grid > div {
  display: flex; flex-direction: column; gap: 2px; padding: 8px 10px; border-radius: 9px;
  background: var(--cl-row); border: 1px solid var(--cl-row-border);
}
.s-grid b { font-size: 15px; color: var(--cl-text); font-variant-numeric: tabular-nums; }
.s-grid span { font-size: 11px; color: var(--cl-text-3); }
.s-err { margin: 0; font-size: 12.5px; color: var(--cl-text-2); }
.s-err b { color: var(--cl-warn-fg); }
.b-head { display: flex; align-items: center; gap: 10px; }
.b-head b { font-size: 14px; color: var(--cl-text); }
.mini-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.mini-list li {
  padding: 9px 11px; border-radius: 10px;
  background: var(--cl-row); border: 1px solid var(--cl-row-border);
  border-left: 3px solid var(--cl-warn);
}
.mini-list li.done { border-left-color: var(--cl-ok); }
.ml-top { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.ml-tag { font-size: 11px; color: var(--cl-text-3); }
.ml-text {
  font-size: 12.5px; line-height: 1.6; color: var(--cl-text-2);
  display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
</style>
