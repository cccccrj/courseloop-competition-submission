import { defineStore } from 'pinia'

// 皮肤偏好。
//
// 用新键 cl_theme_v2 而不是复用 cl_theme：老键是"浅色/深色"两档时代的产物，
// 取值语义（light/dark）和现在的三档对不上，读到一个语义变过的旧值比读不到更麻烦。
// main.ts 里清理老键那行因此保留。
const KEY = 'cl_theme_v2'

export type ThemeMode = 'day' | 'sepia' | 'night'

export interface ThemeMeta {
  value: ThemeMode
  label: string
  hint: string
  icon: string
  /** 首帧底色，须与该档的 --cl-bg 一致 */
  bg: string
}

// 顺序即切换器里的展示顺序：由亮到暗。
// bg 这一列在 index.html 的防闪脚本里有一份镜像 —— 改这里记得改那边，
// 两处对不上的症状是刷新时闪一下别的颜色。
export const THEMES: ThemeMeta[] = [
  { value: 'day', label: '日间', hint: '白底 · 蓝色导航', icon: '☀', bg: '#ffffff' },
  { value: 'sepia', label: '护眼', hint: '米黄纸感 · 低蓝光', icon: '◐', bg: '#f4ece0' },
  { value: 'night', label: '夜间', hint: '深空靛蓝', icon: '☾', bg: '#06080f' },
]

const DEFAULT: ThemeMode = 'day'

function isMode(v: unknown): v is ThemeMode {
  return THEMES.some((t) => t.value === v)
}

/** 隐私模式 / 禁用 Cookie 时 localStorage 的读写都会抛，偏好丢了不该让整站白屏 */
function safeWrite(mode: ThemeMode) {
  try {
    localStorage.setItem(KEY, mode)
  } catch {
    /* 存不下就只在本次会话生效 */
  }
}

/**
 * 初始档位。优先读 <html data-theme> —— index.html 的防闪脚本在首帧之前就已经
 * 依据 localStorage 决定过一次，这里读回它的结论而不是重新决策，避免两处判断
 * 逻辑各自演化后打架（比如脚本认了 sepia、store 又判回 day，画面会闪一下）。
 */
function initial(): ThemeMode {
  const fromDom = document.documentElement.getAttribute('data-theme')
  if (isMode(fromDom)) return fromDom
  // 防闪脚本对默认档不写 data-theme（裸 :root 就是日间），所以这里还要看一眼存储
  try {
    const stored = localStorage.getItem(KEY)
    if (isMode(stored)) return stored
  } catch {
    /* 读不到就走默认 */
  }
  return DEFAULT
}

function apply(mode: ThemeMode) {
  const el = document.documentElement
  // 日间是裸 :root，不打属性：JS 没跑起来时页面也仍然是完整的日间主题
  if (mode === DEFAULT) el.removeAttribute('data-theme')
  else el.setAttribute('data-theme', mode)

  const bg = THEMES.find((t) => t.value === mode)!.bg
  // html 的内联底色只在 body 铺开之前可见（首帧、以及移动端的橡皮筋回弹区），
  // 切换时一并更新，否则回弹时会露出上一档的颜色
  el.style.background = bg
  el.style.colorScheme = mode === 'night' ? 'dark' : 'light'
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', bg)
}

export const useThemeStore = defineStore('theme', {
  state: () => ({ mode: initial() }),
  getters: {
    options: (): ThemeMeta[] => THEMES,
    meta(): ThemeMeta {
      return THEMES.find((t) => t.value === this.mode) || THEMES[0]
    },
  },
  actions: {
    set(mode: ThemeMode) {
      if (mode === this.mode) return
      this.mode = mode
      apply(mode)
      safeWrite(mode)
    },
  },
})
