// SSE 事件帧（后端 POST-SSE，data: {...}\n\n）
export interface SSEEvent {
  type: string // token | think | citation | verify | done | error ...
  [key: string]: unknown
}

export interface CLUser {
  username: string
  role: 'student' | 'ta' | 'teacher' | 'admin'
  name?: string
  // 注册资料。登录与注册两个端点返回同一形状（后端 db._user_payload）。
  // 全部可选：老账号可能没填过这些字段，回读时是空串。
  student_id?: string
  college?: string
  major?: string
  class_name?: string
  email?: string
  phone?: string
  avatar?: string
}

// 教材引用
export interface Citation {
  source: string
  title_path?: string
  page_start?: number
  page_end?: number
  doc_type?: string
  excerpt?: string
}

// 资料入库任务。后端 ingest_jobs 表的一行，字段名保持一致。
// 三个计数是分开的：parsed 是解析出多少块，indexed 是真正写进索引的，
// duplicate 是因内容重复被跳过的 —— 合成一个数就看不出"传了但没进去"。
export interface IngestJob {
  id: number
  filename: string
  course_id: string
  doc_type: 'textbook' | 'exercise' | 'courseware'
  status: 'pending' | 'running' | 'done' | 'failed' | 'withdrawn'
  error: string
  parsed_chunks: number
  indexed_chunks: number
  duplicate_chunks: number
  size_bytes: number
  uploaded_by: string
  created_at: string
  finished_at: string
  // 撤下的时间。撤下不删文件，只是断开检索、移出 data/raw、从默认列表里隐去，
  // 所以这一行会一直留着 —— 空串/null 表示没被撤下过。
  withdrawn_at?: string | null
}
