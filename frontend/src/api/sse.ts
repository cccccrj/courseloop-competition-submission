import { API_BASE, authHeader, describeFailure } from './client'
import type { SSEEvent } from './types'

/**
 * 消费后端 POST + SSE 流式接口。浏览器原生 EventSource 只支持 GET，后端是 POST，
 * 因此用 fetch + ReadableStream 手动按 `data: ...\n\n` 解析。返回一个 abort 函数用于中断。
 */
export function postSSE(
  path: string,
  body: unknown,
  onEvent: (ev: SSEEvent) => void,
  opts?: { onError?: (e: unknown) => void; onDone?: () => void },
): () => void {
  // 与 client.ts 的拦截器走同一份令牌 —— 流式和非流式请求必须是同一个身份，
  // 否则会出现「对话能发但作答不落库」这类只在某一条链路上出现的怪现象。
  const headers: Record<string, string> = { 'Content-Type': 'application/json', ...authHeader() }
  return runSSE(path, { method: 'POST', headers, body: JSON.stringify(body ?? {}) }, onEvent, opts)
}

function runSSE(
  path: string,
  init: RequestInit,
  onEvent: (ev: SSEEvent) => void,
  opts?: { onError?: (e: unknown) => void; onDone?: () => void },
): () => void {
  const controller = new AbortController()

  ;(async () => {
    try {
      let resp: Response
      try {
        resp = await fetch(API_BASE + path, { ...init, signal: controller.signal })
      } catch (e) {
        if ((e as Error)?.name === 'AbortError') throw e
        // fetch 本身抛错 = 没拿到任何响应（断网、服务没起来）
        onEvent({ type: 'error', kind: 'network', message: describeFailure({}) })
        opts?.onError?.(e)
        return
      }
      if (!resp.ok || !resp.body) {
        // 非 2xx：后端的 detail 已经是给用户看的话术（见 client.ts describeFailure），
        // 接口路径只留在控制台。503 = 功能暂未开通（没配模型、题库不够），前端据此给替代入口。
        let data: any = null
        try { data = await resp.json() } catch { /* 不是 JSON 就只按状态码措辞 */ }
        console.error(`[sse] ${path} HTTP ${resp.status}`, data)
        onEvent({
          type: 'error',
          kind: resp.status === 503 ? 'not_configured' : 'http',
          status: resp.status,
          error_id: data?.error_id,
          message: describeFailure({ status: resp.status, detail: data?.detail, errorId: data?.error_id }),
        })
        opts?.onError?.(new Error(`HTTP ${resp.status}`))
        return
      }
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        const parts = buf.split('\n\n')
        buf = parts.pop() ?? ''
        for (const part of parts) {
          const line = part.replace(/^data:\s?/, '').trim()
          if (!line) continue
          try {
            onEvent(JSON.parse(line) as SSEEvent)
          } catch {
            /* 跳过无法解析的片段 */
          }
        }
      }
      opts?.onDone?.()
    } catch (e) {
      if ((e as Error)?.name === 'AbortError') return
      console.error(`[sse] ${path}`, e)   // 路径和原文留在控制台
      onEvent({ type: 'error', kind: 'network', message: '连接中断了，请重试。' })
      opts?.onError?.(e)
    }
  })()

  return () => controller.abort()
}

/** 流式 error 事件 → 界面上的一句话。后端带了错误编号就附上，学生报错时能说出是哪一次。 */
export function sseErrorText(ev: { [key: string]: unknown }, fallback = '这一步没有完成，请重试。'): string {
  const msg = String(ev.message || fallback)
  const id = ev.error_id ? String(ev.error_id) : ''
  return id && !msg.includes(id) ? `${msg}（错误编号 ${id}）` : msg
}
