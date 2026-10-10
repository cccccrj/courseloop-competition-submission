<script setup lang="ts">
import { computed } from 'vue'
import { marked } from 'marked'
import hljs from 'highlight.js/lib/core'
import bash from 'highlight.js/lib/languages/bash'
import c from 'highlight.js/lib/languages/c'
import cpp from 'highlight.js/lib/languages/cpp'
import java from 'highlight.js/lib/languages/java'
import javascript from 'highlight.js/lib/languages/javascript'
import json from 'highlight.js/lib/languages/json'
import python from 'highlight.js/lib/languages/python'
import typescript from 'highlight.js/lib/languages/typescript'
import katex from 'katex'
import 'katex/dist/katex.min.css'
// 不引 highlight.js 自带的主题表。它们是写死的单档配色（原先用的 github-dark
// 连 background 都钉死成 #0d1117），data-theme 切不动 —— 日间/护眼下代码块会是
// 一块黑。语法色改由 theme.css 末尾的 .hljs-* 段承担，取值走 --cl-code-* 令牌，
// 三档各定义一份。那一段只覆盖实际会被打上的高频 token，其余继承正文色。

// 只注册课程答疑中实际会出现的语言。直接引入 highlight.js 会把 190+ 种语言
// 全部打进 Markdown chunk，既拖慢首次进入答疑页，也增加浏览器解析成本。
hljs.registerLanguage('bash', bash)
hljs.registerLanguage('shell', bash)
hljs.registerLanguage('c', c)
hljs.registerLanguage('cpp', cpp)
hljs.registerLanguage('c++', cpp)
hljs.registerLanguage('java', java)
hljs.registerLanguage('javascript', javascript)
hljs.registerLanguage('js', javascript)
hljs.registerLanguage('json', json)
hljs.registerLanguage('python', python)
hljs.registerLanguage('py', python)
hljs.registerLanguage('typescript', typescript)
hljs.registerLanguage('ts', typescript)

// 复用母项目 marked 渲染思路，并新增：代码高亮 + KaTeX 数学（母项目 Vue 侧缺失）
const props = defineProps<{ source: string }>()

marked.setOptions({ gfm: true, breaks: true })

// 规范化模型输出：本地微调模型常把 Markdown/HTML/LaTeX 混着吐（如 \<br>、裸 <br>、
// [x]_补 把下标 _ 当成斜体）。这里在渲染前统一清洗，避免排版错乱。
// 关键：先把数学($..$/$$..$$)与代码(`..`/```..```)段抠出保护，再转义正文里与 CJK
// 相邻的 _ / *（技术中文里几乎都是下标/乘号，绝非 markdown 斜体），最后还原被保护段。
const CJK = '\\u4e00-\\u9fff\\u3400-\\u4dbf\\uff00-\\uffef'
// 只转义**落单**的 _ / *：两侧的 (?![_*]) / (?<![_*]) 把成对标记（** __）排除在外。
// 少了这两个断言，`**复习内容**` 会被逐个转义成 `*\*复习内容\*\*` —— 粗体标记
// 散架，页面上只剩一串裸星号。防误斜体针对的是单个符号（[x]_补、*斜体中文*），
// 成对出现的从来就是作者显式写的粗体，不该动。
const RE_UNDERSCORE = new RegExp(
  `(?<=[${CJK}])([_*])(?![_*])|(?<![_*])([_*])(?=[${CJK}])`, 'g')

function normalizeSource(src: string): string {
  if (!src) return ''
  // 1) <br> 及被转义的 \<br> → 换行（breaks:true 会渲染成 <br>）
  let s = src.replace(/\\?<br\s*\/?>/gi, '\n')
  // 2) 保护数学/代码段（占位符用 NUL 包裹，正文绝不会出现该控制字符）
  const spans: string[] = []
  const mask = (re: RegExp) => {
    s = s.replace(re, (m) => { spans.push(m); return `\x00${spans.length - 1}\x00` })
  }
  mask(/```[\s\S]*?```/g)   // 围栏代码
  mask(/`[^`]*`/g)          // 行内代码
  mask(/\$\$[\s\S]*?\$\$/g) // 块级公式
  mask(/\$[^$\n]*?\$/g)     // 行内公式
  // 3) 转义与 CJK 相邻、且不在数学/代码内的 _ 和 *（防误斜体）
  s = s.replace(RE_UNDERSCORE, (m) => '\\' + m)
  // 4) 还原被保护段
  s = s.replace(/\x00(\d+)\x00/g, (_m, i) => spans[Number(i)] ?? '')
  return s
}

function renderMath(text: string): string {
  // 块级 $$...$$
  text = text.replace(/\$\$([\s\S]+?)\$\$/g, (_m, expr) => {
    try { return katex.renderToString(expr.trim(), { displayMode: true, throwOnError: false }) } catch { return _m }
  })
  // 行内 $...$：允许分隔符与公式间有空格（如「$ x = -13 $」），渲染时 trim；
  // 仍要求内部非纯空白，避免把「$ 」这类误当公式。
  text = text.replace(/\$\s*([^\s$][^$]*?[^\s$]|\S)\s*\$/g, (_m, expr) => {
    try { return katex.renderToString(expr.trim(), { displayMode: false, throwOnError: false }) } catch { return _m }
  })
  return text
}

const html = computed(() => {
  const src = normalizeSource(props.source || '')
  const raw = marked.parse(src, { async: false }) as string
  // 代码高亮
  const withCode = raw.replace(/<pre><code(?: class="language-(\w+)")?>([\s\S]*?)<\/code><\/pre>/g, (_m, lang, code) => {
    const decoded = code.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&').replace(/&quot;/g, '"')
    try {
      const out = lang && hljs.getLanguage(lang)
        ? hljs.highlight(decoded, { language: lang }).value
        : hljs.highlightAuto(decoded).value
      return `<pre class="hljs"><code>${out}</code></pre>`
    } catch {
      return `<pre class="hljs"><code>${code}</code></pre>`
    }
  })
  return renderMath(withCode)
})
</script>

<template>
  <div class="md" v-html="html"></div>
</template>

<style scoped>
/* 兜底防溢出：长串（未渲染的长公式/竖式/无空格 token）强制换行，绝不撑破容器。
   min-width:0 让本组件在 flex/grid 父容器里可以被压缩而非溢出。 */
.md {
  min-width: 0;
  max-width: 100%;
  overflow-wrap: anywhere;
  word-break: break-word;
}
.md :deep(*) { max-width: 100%; }
/* KaTeX 块级公式过宽时横向滚动，而不是顶破卡片 */
.md :deep(.katex-display) { overflow-x: auto; overflow-y: hidden; margin: 10px 0; }
/* KaTeX 默认吃 currentColor，放在 --cl-text-2 的说明文字里公式会跟着变灰。
   公式是结论性内容，钉到正文主色，不跟上下文降级。 */
.md :deep(.katex) { white-space: normal; color: var(--cl-text); }
/* 粗体：同 .katex 的道理 —— 正文常挂在 --cl-text-2（次级灰）的说明区里，
   <strong> 只吃浏览器默认的 bolder 就会是"灰底上略粗一点"，读不出强调。
   字重钉死 700（不用相对值 bolder，父级已偏粗时它可能升不上去），
   颜色提到正文主色：作者显式加粗的都是结论词，不该跟着上下文降级。 */
.md :deep(strong), .md :deep(b) { font-weight: 700; color: var(--cl-text); }
.md :deep(h1), .md :deep(h2), .md :deep(h3) { line-height: 1.4; margin: 14px 0 8px; }
.md :deep(p) { margin: 8px 0; }
/* 行内代码：正文里的 `变量名`。用带一点靛蓝的底而不是灰底，
   免得和 <kbd>、<mark> 这些同样"框一小段字"的标记混为一谈。 */
.md :deep(code) {
  font-family: var(--cl-mono); font-size: 13px;
  background: var(--cl-code-inline-bg); color: var(--cl-brand-700);
  padding: 1px 6px; border-radius: 6px;
}
/* 围栏代码的**版式**（背景、描边、内边距）留在这里，语法色在 theme.css。
   分工：这里管"代码块长什么形状"，那边管"哪个词是什么颜色"。 */
.md :deep(pre.hljs) {
  background: var(--cl-code-bg); border: 1px solid var(--cl-code-border);
  border-radius: var(--cl-radius-sm); padding: 14px 16px; overflow: auto;
  font-size: 13px; line-height: 1.62;
}
.md :deep(pre.hljs code) { background: transparent; color: var(--cl-text); padding: 0; }
/* 这里原先还有一段品牌化覆盖（关键字→靛蓝、字符串→青），已上收到 theme.css
   的 .hljs-* 段。别在本文件重新加语法色：scoped 的 :deep() 权重高于那边的类选择器，
   加回来就等于把某个 token 钉死成单档颜色，切主题时只有它不变。 */

.md :deep(mark) { background: var(--cl-brand-100); color: var(--cl-brand-700); padding: 0 3px; border-radius: 4px; }
.md :deep(blockquote) {
  margin: 10px 0; padding: 4px 14px; border-left: 3px solid var(--cl-brand);
  background: var(--cl-row); border-radius: 0 8px 8px 0; color: var(--cl-text-2);
}
.md :deep(table) { border-collapse: collapse; margin: 10px 0; }
.md :deep(th), .md :deep(td) { border: 1px solid var(--cl-row-border); padding: 6px 12px; }
.md :deep(th) { background: var(--cl-row); font-weight: 700; }
.md :deep(a) { color: var(--cl-brand-600); text-decoration: underline; text-underline-offset: 2px; }
.md :deep(hr) { border: 0; height: 1px; background: var(--cl-glass-hi); margin: 16px 0; }
</style>
