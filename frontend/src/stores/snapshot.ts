/**
 * 本标签页的状态快照：节流写 sessionStorage，离开页面前强制落盘。
 *
 * chat / solve / exam 三个 store 都要这套东西，原先 chat.ts 和 solve.ts 各抄了一份
 * （连注释都一样）。第三份出现的时候把它抽出来 —— 这段逻辑里有两个容易抄错的点，
 * 散在三处早晚会改歪一处：
 *
 *   1. **节流的那次写要拿最新的值**。写是延后 1 秒发生的，序列化的必须是届时的
 *      state，不是排队那一刻的。所以收的是取值函数而不是值。
 *   2. **退出前必须强制刷一次**。快照平时节流写（流式期间每个 token 都序列化整段
 *      对话太贵），而刷新往往正好落在两次写之间 —— 丢的恰恰是「刚生成出来的那几十
 *      个字」，也就是用户最在意的一段。beforeunload / pagehide / visibilitychange
 *      三个事件覆盖刷新、关闭和切到后台。
 *
 * 用 sessionStorage 而不是 localStorage：每个标签页各一份。换成 localStorage 能跨
 * 标签页，但也会让"昨天那段对话"在今天新开的标签页里突然冒出来，反而更怪。
 */

/** 流式期间每个 token 都写一次太贵；1 秒是"丢一句话"和"卡一下"之间的折中。 */
const THROTTLE_MS = 1000

/**
 * 页面要走了。三个事件覆盖刷新、关闭和切到后台 —— 单靠 beforeunload 在移动端
 * 基本不触发，pagehide / visibilitychange 才是那里真正会发生的那一个。
 *
 * 回调必须是同步的：这一刻之后没有下一帧，await 之后的代码不会再执行。
 */
export function onPageExit(fn: () => void) {
  if (typeof window === 'undefined') return
  window.addEventListener('beforeunload', fn)
  window.addEventListener('pagehide', fn)
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') fn()
  })
}

export interface Snapshot<T> {
  /** 读快照。`heal` 用来把上一次页面加载留下的"进行中"落地（那些请求早没了）。 */
  read(heal?: (data: T) => void): T | null
  /** 写快照。`immediate` 跳过节流，用于状态刚发生关键变化时。 */
  write(value: () => T, immediate?: boolean): void
  /** 清掉（退出登录）。连排队中的那次写一起取消，否则它会把刚清的东西再写回去。 */
  clear(): void
}

export function useSnapshot<T>(key: string): Snapshot<T> {
  let timer: ReturnType<typeof setTimeout> | undefined
  let latest: (() => T) | null = null
  let hooked = false

  const flush = () => {
    if (timer) { clearTimeout(timer); timer = undefined }
    if (!latest) return
    try {
      sessionStorage.setItem(key, JSON.stringify(latest()))
    } catch {
      /* 隐私模式 / 配额满：存不下就只是回到"刷新会丢"，不该报错打断正在做的事 */
    }
  }

  const hookExit = () => {
    if (hooked) return
    hooked = true
    onPageExit(flush)
  }

  return {
    read(heal) {
      try {
        const raw = sessionStorage.getItem(key)
        if (!raw) return null
        const data = JSON.parse(raw) as T
        heal?.(data)
        return data
      } catch {
        // 存的是上个版本的结构、或者被别的东西写坏了：当作没有快照，
        // 而不是让整个页面在读缓存这一步崩掉。
        return null
      }
    },

    write(value, immediate = false) {
      latest = value
      hookExit()
      if (immediate) { flush(); return }
      if (timer) return
      timer = setTimeout(flush, THROTTLE_MS)
    },

    clear() {
      if (timer) { clearTimeout(timer); timer = undefined }
      latest = null
      try { sessionStorage.removeItem(key) } catch { /* ignore */ }
    },
  }
}
