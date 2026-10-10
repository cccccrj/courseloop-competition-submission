<script setup lang="ts">
/**
 * 资料入库（教务）：选课程 → 选文件 → 上传 → 看解析入库结果 → 必要时撤下。
 *
 * 这是 /admin/* 里唯一一个"不做页面就等于没有"的功能。课程与账号一学期配一次，
 * 走脚本尚可；资料是要持续补充的，老师不会为了传一份课件去学 curl。
 *
 * 四条渲染纪律：
 *
 * 1) **失败要说人话，并且留在页面上**。解析出 0 块（扫描件没有文本层）是这里
 *    最常见的失败，它和"文件损坏"是两种完全不同的处置。后端把原因原样存进
 *    任务行，这里就原样显示，不折叠、不省略、不换成"上传失败"。
 * 2) **三个计数分开显示**。解析 48 / 入库 41 / 重复 7 —— 合成一个数就看不出
 *    "传进去了但没进索引"。重复不是错误，是去重生效了，所以它是中性色。
 * 3) **撤下不是删除**。撤下断开检索、把文件移出 data/raw、从默认列表隐去，
 *    但**原件留在服务器上**，随时能放回。后端对同名文件返回 409（不静默覆盖，
 *    否则两版内容会同时被检索到且标着同一个出处），撤下就是那个 409 的出路。
 * 4) **前端先挡一道**。后缀与大小在选文件时就判掉，不让人等一份 80 MB 的
 *    文件传完再看到 413。这不是替代后端校验，后端那道仍然是权威。
 */
import { computed, onActivated, onDeactivated, ref, watch } from 'vue'
import { del, get, post } from '@/api/client'
import { toast } from '@/api/toast'
import type { IngestJob } from '@/api/types'
import { useCourseStore } from '@/stores/course'
import CLPageHeader from '@/components/ui/CLPageHeader.vue'
import CLBadge from '@/components/ui/CLBadge.vue'
import CLButton from '@/components/ui/CLButton.vue'
import CLCard from '@/components/ui/CLCard.vue'
import CLEmpty from '@/components/ui/CLEmpty.vue'
import CLSegmented from '@/components/ui/CLSegmented.vue'
import CLSkeleton from '@/components/ui/CLSkeleton.vue'

const courseStore = useCourseStore()

// 与后端 _EXT_PARSER / MAX_UPLOAD_BYTES 对齐。写死一份副本是有意的：
// 让用户在点"上传"之前就知道传不进去，比等一趟往返再报错要好。
const ACCEPT = ['.pdf', '.pptx', '.ppt', '.docx', '.txt']
const MAX_BYTES = 80 * 1024 * 1024

// 没有页码的格式。Word 和纯文本按标题切块，引用里给到的是「第3章 / 3.2 CSMA/CD」
// 而不是「第 87 页」—— 这件事要在传之前就说，不能等老师回头问"为什么没有页码"。
const NO_PAGE = ['.docx', '.txt']

const DOC_TYPES = [
  { label: '课件', value: 'courseware' },
  { label: '教材', value: 'textbook' },
  { label: '习题', value: 'exercise' },
]
const DOC_LABEL: Record<string, string> = {
  courseware: '课件', textbook: '教材', exercise: '习题',
}

const courseId = ref(courseStore.current)
const docType = ref('courseware')
const picked = ref<File | null>(null)
const uploading = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

const jobs = ref<IngestJob[]>([])
const loading = ref(true)
const filter = ref('')          // '' = 全部课程
const view = ref('active')      // active = 在库；withdrawn = 已撤下（文件仍在服务器上）
const VIEWS = [{ label: '在库', value: 'active' }, { label: '已撤下', value: 'withdrawn' }]
const isWithdrawnView = computed(() => view.value === 'withdrawn')

const courseOptions = computed(() =>
  courseStore.list.map((c) => ({ label: c.short, value: c.id })))
const courseName = (id: string) =>
  courseStore.list.find((c) => c.id === id)?.short || id

const pickedNoPage = computed(() => {
  const n = picked.value?.name.toLowerCase() || ''
  return NO_PAGE.some((ext) => n.endsWith(ext))
})

// 还在跑的任务。有它就要继续轮询 —— 解析建索引是后台任务，
// 页面不主动回来问就会一直停在 running 上。
const busy = computed(() => jobs.value.some((j) => j.status === 'pending' || j.status === 'running'))

let timer: ReturnType<typeof setTimeout> | null = null

async function load(quiet = false) {
  if (!quiet) loading.value = true
  try {
    const q = new URLSearchParams()
    if (filter.value) q.set('course_id', filter.value)
    if (isWithdrawnView.value) q.set('withdrawn', 'true')
    const qs = q.toString()
    jobs.value = (await get<{ items: IngestJob[] }>(`/admin/materials${qs ? `?${qs}` : ''}`)).items
  } catch (e) {
    toast(detail(e, '资料列表读取失败'), 'error')
  } finally {
    loading.value = false
  }
  schedule()
}

// 只在有任务在跑时轮询，跑完就停。常驻轮询对一个"一学期用几次"的页面
// 是纯粹的浪费，而且会让后台日志里全是这条请求。
function schedule() {
  if (timer) clearTimeout(timer)
  timer = busy.value ? setTimeout(() => load(true), 2000) : null
}

// 换筛选项就重读。用 watch 而不是在 CLSegmented 上多挂一个 @update:model-value：
// 那样 v-model 的赋值与这个回调的先后顺序要靠模板编译器的实现细节，
// 一旦顺序反过来就会用旧的 filter 值去请求，且症状是"要点两次才对"。
watch([filter, view], () => load())

onActivated(() => { courseStore.load(); load() })
onDeactivated(() => { if (timer) clearTimeout(timer); timer = null })

// 后端的 detail 就是给人看的操作指引（"已有同名文件，请先改名或删除旧版"），
// 原样透出来；client.ts 已对 /admin/materials 静音，不会重复弹一条。
function detail(e: unknown, fallback: string): string {
  const err = e as { response?: { data?: { detail?: string } } }
  return err?.response?.data?.detail || fallback
}

function fmtSize(n: number): string {
  if (!n) return '—'
  return n >= 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`
}

function fmtTime(s: string): string {
  return (s || '').replace('T', ' ').slice(5, 16) || '—'
}

function pick() {
  fileInput.value?.click()
}

function onPicked(e: Event) {
  const input = e.target as HTMLInputElement
  const f = input.files?.[0] || null
  input.value = ''            // 选同一个文件两次也要能触发 change
  if (!f) return
  const ext = f.name.slice(f.name.lastIndexOf('.')).toLowerCase()
  if (!ACCEPT.includes(ext)) {
    // .doc 单独说：它看着就是 Word，被拒时最容易让人以为"你们不支持 Word"
    toast(ext === '.doc'
      ? '老版 .doc 读不了，请用 Word 另存为 .docx 再传'
      : `只支持 ${ACCEPT.join(' / ')}；扫描件请先 OCR 成数字版 PDF 再传`, 'error')
    return
  }
  if (f.size > MAX_BYTES) {
    toast(`${f.name} 有 ${fmtSize(f.size)}，超过 80 MB 上限`, 'error')
    return
  }
  picked.value = f
}

async function upload() {
  if (!picked.value || uploading.value) return
  uploading.value = true
  const form = new FormData()
  form.append('file', picked.value)
  form.append('course_id', courseId.value)
  form.append('doc_type', docType.value)
  try {
    await post('/admin/materials', form)
    toast(`${picked.value.name} 已上传，正在解析入库`)
    picked.value = null
    if (filter.value && filter.value !== courseId.value) filter.value = courseId.value
    await load(true)
  } catch (e) {
    toast(detail(e, '上传失败'), 'error')
  } finally {
    uploading.value = false
  }
}

async function withdraw(job: IngestJob) {
  const warn = job.indexed_chunks
    ? `学生将不再能检索或引用到它（从检索库里移除 ${job.indexed_chunks} 个分块）。`
    : '它在检索库里没有分块（当初解析失败），撤下只是腾出这个文件名。'
  // 说清楚文件没被删：不然教务会以为撤下等于销毁，该撤的也不敢撤
  if (!confirm(`撤下《${job.filename}》？\n\n${warn}\n\n原件仍保留在服务器上，`
    + '可以在「已撤下」里随时放回。')) return
  try {
    const r = await del<{ deleted_chunks: number }>(`/admin/materials/${job.id}`)
    toast(`已撤下 ${job.filename}（移除 ${r.deleted_chunks} 个分块，原件保留）`)
    await load(true)
  } catch (e) {
    toast(detail(e, '撤下失败'), 'error')
  }
}

async function restore(job: IngestJob) {
  try {
    await post(`/admin/materials/${job.id}/restore`)
    toast(`${job.filename} 已放回，正在重新解析入库`)
    // 重新入库要跑一会儿，切回在库视图才看得到它的进度
    view.value = 'active'
    await load(true)
  } catch (e) {
    toast(detail(e, '放回失败'), 'error')
  }
}

const STATUS: Record<string, { label: string; variant: 'ok' | 'warn' | 'err' | 'neutral' }> = {
  done: { label: '已入库', variant: 'ok' },
  failed: { label: '失败', variant: 'err' },
  running: { label: '解析中', variant: 'warn' },
  pending: { label: '排队中', variant: 'warn' },
  withdrawn: { label: '已撤下', variant: 'neutral' },
}
</script>

<template>
  <section class="page">
    <CLPageHeader title="资料入库" sub="上传的课件和教材会成为 AI 回答时引用的依据">
      <template #actions>
        <CLBadge variant="cite">{{ jobs.length }} 条记录</CLBadge>
        <CLBadge v-if="busy" variant="warn">有任务在跑</CLBadge>
      </template>
    </CLPageHeader>

    <CLCard>
      <template #header>
        <div>
          <div class="ctitle">上传一份资料</div>
          <div class="csub">
            接受 <b>Word（.docx）</b>、<b>纯文本（.txt）</b>、<b>数字版 PDF</b> 与 <b>PPT</b>，单份 80 MB 以内。
          </div>
          <ul class="notes">
            <li><b>数字版 PDF</b> 指能用鼠标选中文字的那种。扫描件（整页是图片）解析不出内容，
              传上来会落成失败并写明原因，需先 OCR 成数字版再传。</li>
            <li><b>Word 与 txt 没有页码</b>，引用只能给到标题路径（如「第3章 / 3.2 CSMA/CD」），
              不会出现「第 87 页」。要页码就传数字版 PDF。</li>
            <li>老版 <b>.doc</b> 读不了，用 Word 另存为 .docx 即可。</li>
            <li><b>撤下不会删文件</b>。撤下只是断开检索、从这个列表里隐去，原件仍在服务器上，
              可以在「已撤下」里随时放回。</li>
          </ul>
        </div>
      </template>
      <div class="form">
        <div class="row">
          <div class="lbl">课程</div>
          <CLSegmented v-model="courseId" :options="courseOptions" />
        </div>
        <div class="row">
          <div class="lbl">类别</div>
          <CLSegmented v-model="docType" :options="DOC_TYPES" />
        </div>
        <div class="row">
          <div class="lbl">文件</div>
          <div class="filebox">
            <input ref="fileInput" class="hidden-input" type="file" :accept="ACCEPT.join(',')" @change="onPicked" />
            <CLButton variant="ghost" @click="pick">选择文件…</CLButton>
            <span v-if="picked" class="picked">{{ picked.name }}<i>{{ fmtSize(picked.size) }}</i></span>
            <span v-else class="picked muted">尚未选择</span>
            <CLBadge v-if="pickedNoPage" variant="warn">引用无页码</CLBadge>
          </div>
        </div>
        <div class="row">
          <div class="lbl"></div>
          <CLButton :disabled="!picked || uploading" @click="upload">
            {{ uploading ? '上传中…' : '上传并入库' }}
          </CLButton>
        </div>
      </div>
    </CLCard>

    <CLCard :title="isWithdrawnView ? '已撤下' : '入库记录'"
      :sub="isWithdrawnView
        ? '这些资料已断开检索，学生看不到；原件仍在服务器上，放回后会重新解析入库。'
        : '解析 / 入库 / 重复三个数分开看：重复是去重生效跳过的块，不是错误。'">
      <template #actions>
        <CLSegmented v-model="view" :options="VIEWS" />
        <CLSegmented v-model="filter" :options="[{ label: '全部', value: '' }, ...courseOptions]" />
      </template>

      <CLSkeleton v-if="loading" :rows="4" />
      <CLEmpty v-else-if="!jobs.length"
        :title="isWithdrawnView ? '没有撤下过资料' : '还没有传过资料'"
        :hint="isWithdrawnView
          ? '撤下的资料会停在这里，原件不删，随时可以放回。'
          : '上面选一份课件传上来，解析完学生就能在回答里引用到它的页码。'" />
      <div v-else class="tbl-wrap">
        <table class="tbl">
          <thead>
            <tr>
              <th>文件</th><th>课程</th><th>类别</th><th>状态</th>
              <th class="num">解析</th><th class="num">入库</th><th class="num">重复</th>
              <th>{{ isWithdrawnView ? '撤下于' : '上传' }}</th><th></th>
            </tr>
          </thead>
          <tbody>
            <template v-for="j in jobs" :key="j.id">
              <tr>
                <td class="fname">{{ j.filename }}<i>{{ fmtSize(j.size_bytes) }}</i></td>
                <td>{{ courseName(j.course_id) }}</td>
                <td>{{ DOC_LABEL[j.doc_type] || j.doc_type }}</td>
                <td><CLBadge :variant="STATUS[j.status]?.variant || 'neutral'">{{ STATUS[j.status]?.label || j.status }}</CLBadge></td>
                <td class="num">{{ j.parsed_chunks || '—' }}</td>
                <td class="num strong">{{ j.indexed_chunks || '—' }}</td>
                <td class="num muted">{{ j.duplicate_chunks || '—' }}</td>
                <td class="muted">
                  {{ fmtTime(isWithdrawnView ? (j.withdrawn_at || '') : j.created_at) }}
                  <i>{{ j.uploaded_by }}</i>
                </td>
                <td class="act">
                  <button v-if="isWithdrawnView" class="plain" title="放回资料库并重新入库"
                    @click="restore(j)">放回</button>
                  <button v-else class="danger" :disabled="j.status === 'pending' || j.status === 'running'"
                    :title="j.status === 'running' ? '入库中不能撤下' : '断开检索，原件保留在服务器上'"
                    @click="withdraw(j)">撤下</button>
                </td>
              </tr>
              <!-- 失败原因单独占一行、原样显示。塞进 title 或截断成一行，
                   教务就只能看到"失败"两个字，不知道该 OCR 还是该换文件。 -->
              <tr v-if="j.error" class="err-row">
                <td colspan="9"><span class="err-tag">失败原因</span>{{ j.error }}</td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
    </CLCard>
  </section>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 18px; }

.form { display: flex; flex-direction: column; gap: 14px; }
/* 卡头用了具名 slot，标题与副标题的字号在这里对齐 CLCard 自带的那一套 */
.ctitle { font-size: 15px; font-weight: 700; color: var(--cl-text); }
.csub { margin-top: 4px; font-size: 13px; color: var(--cl-text-2); line-height: 1.6; }
.csub b, .notes b { color: var(--cl-text); font-weight: 700; }
/* 三条限制写成列表而不是塞进一句副标题：它们是三件不同的事，
   连成一段话时老师只会读到第一句 */
.notes { margin: 8px 0 0; padding-left: 18px; display: flex; flex-direction: column; gap: 4px; }
.notes li { font-size: 12.5px; color: var(--cl-text-3); line-height: 1.6; }
.row { display: flex; align-items: center; gap: 14px; }
.lbl { width: 44px; flex-shrink: 0; font-size: 13px; font-weight: 700; color: var(--cl-text-3); }
.filebox { display: flex; align-items: center; gap: 12px; min-width: 0; }
/* 原生 file 控件的按钮样式各浏览器不同，且无法与 CLButton 对齐，
   因此藏起来只留它的文件选择能力，外观交给 CLButton。 */
.hidden-input { display: none; }
.picked { font-size: 13px; color: var(--cl-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.picked.muted { color: var(--cl-text-3); }
.picked i { margin-left: 8px; font-style: normal; font-size: 12px; color: var(--cl-text-3); }

.tbl-wrap { overflow-x: auto; }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th {
  text-align: left; font-weight: 700; font-size: 12px; white-space: nowrap;
  color: var(--cl-text-3); padding: 8px 10px; border-bottom: 1px solid var(--cl-row-border);
}
.tbl td { padding: 10px; border-bottom: 1px solid var(--cl-row-border); color: var(--cl-text-2); vertical-align: middle; }
.tbl tbody tr:hover { background: var(--cl-row-hover); }
.tbl .num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.tbl .num.strong { color: var(--cl-text); font-weight: 700; }
.tbl .muted { color: var(--cl-text-3); }
.fname { color: var(--cl-text); font-weight: 600; max-width: 280px; }
.fname i, .tbl td.muted i { display: block; font-style: normal; font-size: 12px; color: var(--cl-text-3); font-weight: 400; margin-top: 2px; }

/* 失败行不靠 hover 才显形：它是这张表上最需要被读到的东西 */
.err-row td { color: var(--cl-err-fg); background: var(--cl-err-bg); font-size: 12.5px; line-height: 1.6; }
.err-tag {
  display: inline-block; margin-right: 8px; padding: 1px 8px; border-radius: var(--cl-radius-pill);
  border: 1px solid var(--cl-err-bd); font-weight: 700; font-size: 11px;
}

.act { text-align: right; white-space: nowrap; }
.danger {
  padding: 5px 12px; border-radius: var(--cl-radius-sm); cursor: pointer; font: inherit; font-size: 12px; font-weight: 600;
  background: transparent; border: 1px solid var(--cl-border); color: var(--cl-text-3);
  transition: color .12s ease, border-color .12s ease, background .12s ease;
}
.danger:hover:not(:disabled) { color: var(--cl-err-fg); border-color: var(--cl-err-bd); background: var(--cl-err-bg); }
.danger:disabled { cursor: not-allowed; opacity: .45; }
.plain {
  padding: 5px 12px; border-radius: var(--cl-radius-sm); cursor: pointer; font: inherit; font-size: 12px; font-weight: 600;
  background: transparent; border: 1px solid var(--cl-border); color: var(--cl-text-2);
  transition: color .12s ease, border-color .12s ease, background .12s ease;
}
.plain:hover { color: var(--cl-cite-fg); border-color: var(--cl-cite-bd); background: var(--cl-cite-bg); }

@media (max-width: 620px) {
  .row { flex-direction: column; align-items: stretch; gap: 8px; }
  .lbl { width: auto; }
}
</style>
