import axios, { type AxiosInstance } from 'axios'
import { toast } from './toast'

// 后端大模型推理较慢，超时设大一些
const TIMEOUT = 600_000

export const API_BASE = import.meta.env.VITE_API_BASE || '/api'

const http: AxiosInstance = axios.create({
  baseURL: API_BASE,
  timeout: TIMEOUT,
  headers: { 'Content-Type': 'application/json' },
})

// 令牌存放位置。与 cl_user 分开存：cl_user 只是给界面显示用的资料快照，
// 可以随便读改；cl_token 才是授权凭据，改了它只会导致验签失败。
export const TOKEN_KEY = 'cl_token'

export function authHeader(): Record<string, string> {
  try {
    const token = localStorage.getItem(TOKEN_KEY)
    return token ? { Authorization: `Bearer ${token}` } : {}
  } catch {
    return {}
  }
}

// 请求拦截：附带签名令牌。
// 原先发的是明文 X-User / X-Role，等于把「我是谁」交给客户端自己说 ——
// 任何人写个 X-Role: ta 就能读全班数据。现在身份由后端签名，改不动。
http.interceptors.request.use((config) => {
  const header = authHeader()
  if (header.Authorization) config.headers.set?.('Authorization', header.Authorization)
  return config
})

/** 把一次请求失败翻译成学生/助教能照做的一句话。
 *
 * 判据只有一条：后端是不是已经把话说明白了。4xx 的 detail 都是我们自己写的
 * 业务措辞（「题库里只有 8 道，至少需要 20 道」「仅助教/教师/教务可操作」），
 * 那种原样透传最有用。5xx 现在也一样：后端的全局异常处理（backend/core/errors.py）
 * 只外发翻译过的话术并附错误编号，异常原文只进日志。超时和断网连响应都没有，
 * 那两类才需要我们替它措辞。
 *
 * axios 与流式（sse.ts）两条通道共用这一个函数 —— 此前只有 axios 这条改过，
 * 流式那条仍在报「流式请求失败 [/tutor/submit/stream]：HTTP 503」（docs/34 §2.2）。 */
export interface FailureInfo {
  status?: number
  detail?: string
  errorId?: string
  timeout?: boolean
}
export function describeFailure(f: FailureInfo): string {
  const suffix = f.errorId ? `（错误编号 ${f.errorId}）` : ''
  if (f.status === undefined) {
    // 压根没拿到响应：要么超时，要么网络断了。两者的下一步动作不一样。
    return f.timeout
      ? '这一步等太久了，请稍后重试。'
      : '连不上服务器，请检查网络后重试。'
  }
  const s = f.status
  if (s === 403) return (f.detail || '你没有权限执行这个操作。') + suffix
  if (s === 404) return (f.detail || '要找的内容不存在，可能已被删除。') + suffix
  if (s === 429) return '操作太频繁了，请稍等一会儿再试。'
  if (s >= 400 && s < 500) return (f.detail || '这个请求没能完成，请检查后重试。') + suffix
  return (f.detail || '服务暂时不可用，请稍后重试。') + suffix
}

function friendlyError(err: any): string {
  const data = err?.response?.data
  return describeFailure({
    status: err?.response ? err.response.status : undefined,
    detail: typeof data?.detail === 'string' ? data.detail : undefined,
    errorId: data?.error_id,
    timeout: err?.code === 'ECONNABORTED' || /timeout/i.test(err?.message || ''),
  })
}

// 响应拦截：统一错误提示
http.interceptors.response.use(
  (resp) => resp,
  (err) => {
    if (err?.code === 'ERR_CANCELED' || err?.name === 'CanceledError') {
      return Promise.reject(err)
    }
    const path = err?.config?.url ?? ''
    // 令牌过期或失效：清掉本地会话并回登录页。不这么做的话，用户会停在一个
    // 每个请求都弹「未登录」的页面上，却没有任何线索该去哪里重新登录。
    // 这里直接操作 localStorage 而不 import store/router —— client.ts 是它们的
    // 上游依赖，反向引用会绕成循环。
    if (err?.response?.status === 401 && !String(path).startsWith('/auth/login')) {
      try {
        localStorage.removeItem(TOKEN_KEY)
        localStorage.removeItem('cl_user')
      } catch {
        /* ignore */
      }
      if (typeof window !== 'undefined' && !window.location.pathname.startsWith('/login')) {
        toast('登录状态已失效，请重新登录', 'error')
        window.location.assign('/login')
        return Promise.reject(err)
      }
    }
    // /auth/ 与 /admin/materials 完全交给页面自己措辞：这两处的失败原因
    // （用户名已存在、同名文件、超大、扫描件没有文本层）本身就是操作指引，
    // 页面会把它放在表单旁边，这里再弹一条只会重复。
    const silent = /\/log\//.test(path) || String(path).startsWith('/auth/')
      || String(path).startsWith('/admin/materials')
    // 原先这里弹的是「后端调用失败 [/community/standings]：<detail>」—— 接口路径和
    // 后端原文一起怼到学生脸上，既看不懂也没法处置。现在按状态码分档：后端自己写清楚
    // 了原因的（4xx 的 detail 就是给用户看的操作指引，比如题库不足、无权操作）原样透传，
    // 说不清的（5xx、超时、断网）换成一句能照做的话。路径只留给控制台。
    if (!silent) toast(friendlyError(err), 'error')
    if (err?.response?.status >= 500 || !err?.response) {
      console.error(`[api] ${path}`, err)   // 路径和原文留在这里，出事了还查得到
    }
    return Promise.reject(err)
  },
)

export function isCanceled(e: unknown): boolean {
  const err = e as { code?: string; name?: string }
  return err?.code === 'ERR_CANCELED' || err?.name === 'CanceledError'
}

export async function post<T = unknown>(path: string, payload?: unknown, signal?: AbortSignal): Promise<T> {
  const isForm = typeof FormData !== 'undefined' && payload instanceof FormData
  const config: Record<string, unknown> = isForm ? { headers: { 'Content-Type': undefined } } : {}
  if (signal) config.signal = signal
  const r = await http.post(path, payload ?? {}, config)
  return r.data as T
}

/** 下载一份服务端生成的文件（CSV 导出用）。
 *
 * 单独写而不是复用 get()：那个走 JSON 反序列化，拿到的会是被当成字符串的 CSV，
 * 而且拿不到 Content-Disposition 里的文件名。这里按 blob 收，文件名优先用
 * 服务端给的 `filename*`（RFC 5987，中文文件名只能走它），取不到再退回调用方给的。
 */
export async function download(path: string, fallbackName: string): Promise<void> {
  const r = await http.get(path, { responseType: 'blob' })
  const disp = String(r.headers['content-disposition'] || '')
  const star = /filename\*=UTF-8''([^;]+)/i.exec(disp)
  const name = star ? decodeURIComponent(star[1]) : fallbackName
  const url = URL.createObjectURL(r.data as Blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  // 不撤销的话这个 blob 会一直占着内存，导十几次就很可观
  URL.revokeObjectURL(url)
}

export async function get<T = unknown>(path: string): Promise<T> {
  const r = await http.get(path)
  return r.data as T
}

export async function put<T = unknown>(path: string, payload?: unknown): Promise<T> {
  const r = await http.put(path, payload ?? {})
  return r.data as T
}

export async function del<T = unknown>(path: string): Promise<T> {
  const r = await http.delete(path)
  return r.data as T
}

export default http
