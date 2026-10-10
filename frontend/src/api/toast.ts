// 极简全局提示（替代母项目 Element Plus 的 ElMessage，保持零依赖的轻量实现）
// 样式在 theme.css 的 .cl-toast，是全站少数几个用 backdrop-filter 的浮层之一。
let wrap: HTMLElement | null = null

function ensureWrap(): HTMLElement {
  if (!wrap) {
    wrap = document.createElement('div')
    wrap.className = 'cl-toast-wrap'
    document.body.appendChild(wrap)
  }
  return wrap
}

export function toast(message: string, type: 'info' | 'error' = 'info', ms = 2600) {
  const el = document.createElement('div')
  el.className = 'cl-toast' + (type === 'error' ? ' error' : '')
  el.textContent = message
  ensureWrap().appendChild(el)
  setTimeout(() => el.remove(), ms)
}
