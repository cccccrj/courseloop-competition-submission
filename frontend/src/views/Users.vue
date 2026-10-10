<script setup lang="ts">
/**
 * 账号管理（教务）：看名单 → 建号 / 批量导入 → 改角色、停用、改班。
 *
 * 为什么现在才做这个页面：`/admin/users` 后端一直是完整的，但没有界面 ——
 * `scripts/manage_users.py` 的说明里写着"之后的账号可以由管理员在 /admin/users 建"，
 * 而那个页面并不存在。一学期配一次的时候还能忍，要开一个班就忍不了了。
 *
 * 四条渲染纪律（与 Materials.vue 同一套，理由也一样）：
 *
 * 1) **导入先预览再执行**。后端默认 dry-run，这里也就分成两步按钮。一次导入
 *    30 个账号，看不到"将建几个、跳过几个、为什么跳过"就按下去，出错之后
 *    没有干净的回退方式 —— 建出来的账号无法批量撤销。
 * 2) **初始密码必须显示出来**。系统没有邮件能力，这一列是教务把账号发给学生的
 *    唯一渠道。不显示就等于建了一批没人知道密码的号。
 * 3) **失败原因逐行显示，不折叠**。"学号已注册过"和"本次文件内重复"是两种
 *    完全不同的处置：一个要去查库，一个要去改名单。
 * 4) **危险操作说清后果再确认**。停用会让对方的令牌立刻失效（不是等 30 天过期），
 *    这一点要写在确认框里。
 */
import { computed, onActivated, ref } from 'vue'
import { get, post, put } from '@/api/client'
import { toast } from '@/api/toast'
import CLPageHeader from '@/components/ui/CLPageHeader.vue'
import CLBadge from '@/components/ui/CLBadge.vue'
import CLButton from '@/components/ui/CLButton.vue'
import CLCard from '@/components/ui/CLCard.vue'
import CLEmpty from '@/components/ui/CLEmpty.vue'
import CLField from '@/components/ui/CLField.vue'
import CLSegmented from '@/components/ui/CLSegmented.vue'
import CLSelect from '@/components/ui/CLSelect.vue'
import CLSkeleton from '@/components/ui/CLSkeleton.vue'

interface AdminUser {
  username: string; role: string; name: string; status: string
  student_id: string; college: string; major: string; class_name: string
  created_at: string
}
interface ImportRow {
  line: number; username: string; name: string; class_name: string
  ok: boolean; reason: string; password?: string
}
interface ImportResult {
  applied: boolean; total: number; created: number; skipped: number
  warning: string; note: string; items: ImportRow[]
}

const ROLE_LABEL: Record<string, string> = {
  student: '学生', ta: '助教', teacher: '教师', admin: '教务管理员',
}
const ROLE_OPTIONS = [{ label: '全部角色', value: '' },
  ...Object.entries(ROLE_LABEL).map(([value, label]) => ({ label, value }))]
const STATUS_OPTIONS = [
  { label: '全部', value: '' }, { label: '启用中', value: 'active' }, { label: '已停用', value: 'disabled' },
]

const items = ref<AdminUser[]>([])
const byRole = ref<Record<string, number>>({})
const loading = ref(true)
const role = ref('')
const status = ref('')
const keyword = ref('')
const classes = ref<string[]>([])

// 建号表单。密码下限 8 位是后端 CreateUserReq 的约束（比公网注册的 6 位更严），
// 这里写死一份副本，让人在点提交之前就知道，而不是等一趟往返再被拒。
const nu = ref({ username: '', password: '', name: '', role: 'student', student_id: '', class_name: '' })
const creating = ref(false)

const picked = ref<File | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const importing = ref(false)
const preview = ref<ImportResult | null>(null)
const defaultPassword = ref('')

const selected = ref<Set<string>>(new Set())
const targetClass = ref('')

const classGroups = computed(() => [{ label: '已有班级', options: classes.value }])
const students = computed(() => items.value.filter((u) => u.role === 'student'))
const allChecked = computed(() =>
  students.value.length > 0 && students.value.every((u) => selected.value.has(u.username)))

function detail(e: unknown, fallback: string): string {
  const err = e as { response?: { data?: { detail?: string } } }
  return err?.response?.data?.detail || fallback
}

async function load() {
  loading.value = true
  try {
    const q = new URLSearchParams()
    if (role.value) q.set('role', role.value)
    if (status.value) q.set('status', status.value)
    if (keyword.value.trim()) q.set('q', keyword.value.trim())
    const qs = q.toString()
    const body = await get<{ items: AdminUser[]; by_role: Record<string, number> }>(
      `/admin/users${qs ? `?${qs}` : ''}`)
    items.value = body.items
    byRole.value = body.by_role
    // 选中项按新名单收敛：筛掉的人还留在选中集合里，批量改班会改到看不见的人身上
    selected.value = new Set([...selected.value].filter(
      (u) => body.items.some((it) => it.username === u)))
  } catch (e) {
    toast(detail(e, '账号列表读取失败'), 'error')
  } finally {
    loading.value = false
  }
  classes.value = (await get<{ items: string[] }>('/classes').catch(() => ({ items: [] }))).items
}

onActivated(load)

async function create() {
  if (creating.value) return
  creating.value = true
  try {
    await post('/admin/users', { ...nu.value })
    toast(`已建号 ${nu.value.username}（${ROLE_LABEL[nu.value.role]}）`)
    nu.value = { username: '', password: '', name: '', role: 'student', student_id: '', class_name: '' }
    await load()
  } catch (e) {
    toast(detail(e, '建号失败'), 'error')
  } finally {
    creating.value = false
  }
}

async function setRole(u: AdminUser, next: string) {
  if (next === u.role) return
  if (!confirm(`把「${u.name || u.username}」从${ROLE_LABEL[u.role]}改成${ROLE_LABEL[next]}？\n\n`
    + '角色写在令牌里，对方重新登录后才生效。')) return
  try {
    await put(`/admin/users/${encodeURIComponent(u.username)}`, { role: next })
    toast(`${u.username} 已改为${ROLE_LABEL[next]}`)
    await load()
  } catch (e) {
    toast(detail(e, '改角色失败'), 'error')
  }
}

async function toggleStatus(u: AdminUser) {
  const next = u.status === 'active' ? 'disabled' : 'active'
  // 停用不是"下次登录才拦"：现有令牌立刻失效。这句必须写出来 ——
  // 停用通常是安全动作（人离校了、号被人拿到了），教务要知道它立刻生效。
  const warn = next === 'disabled'
    ? '对方当前的登录状态会立刻失效，不是等令牌过期。学习记录全部保留，随时可以重新启用。'
    : '对方可以重新登录，此前的学习记录原样还在。'
  if (!confirm(`${next === 'disabled' ? '停用' : '启用'}「${u.name || u.username}」？\n\n${warn}`)) return
  try {
    await put(`/admin/users/${encodeURIComponent(u.username)}`, { status: next })
    toast(`${u.username} 已${next === 'disabled' ? '停用' : '启用'}`)
    await load()
  } catch (e) {
    toast(detail(e, '操作失败'), 'error')
  }
}

function pick() { fileInput.value?.click() }

function onPicked(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0]
  ;(e.target as HTMLInputElement).value = ''
  if (!f) return
  if (!/\.csv$/i.test(f.name)) { toast('请选择 .csv 名单文件', 'error'); return }
  if (f.size > 2 * 1024 * 1024) { toast('名单文件不应超过 2 MB', 'error'); return }
  picked.value = f
  preview.value = null
}

async function runImport(apply: boolean) {
  if (!picked.value || importing.value) return
  if (apply && !confirm(`确认建号？将新建 ${preview.value?.created ?? 0} 个学生账号。\n\n`
    + '初始密码只在结果表里显示一次，系统不会发邮件，请记下后再发给学生，'
    + '离开这个页面就看不到了。')) return
  importing.value = true
  const form = new FormData()
  form.append('file', picked.value)
  form.append('apply', String(apply))
  form.append('default_password', defaultPassword.value)
  try {
    preview.value = await post<ImportResult>('/admin/users/import', form)
    if (apply) {
      toast(`已建号 ${preview.value.created} 个，跳过 ${preview.value.skipped} 个`)
      await load()
    }
  } catch (e) {
    toast(detail(e, '名单解析失败'), 'error')
  } finally {
    importing.value = false
  }
}

function toggleAll() {
  if (allChecked.value) selected.value = new Set()
  else selected.value = new Set(students.value.map((u) => u.username))
}

function toggleOne(username: string) {
  const next = new Set(selected.value)
  if (next.has(username)) next.delete(username)
  else next.add(username)
  selected.value = next
}

async function applyClass() {
  const names = [...selected.value]
  if (!names.length) return
  const to = targetClass.value.trim()
  if (!confirm(`把选中的 ${names.length} 个学生改到「${to || '（无班级）'}」？\n\n`
    + '班级决定他们出现在助教工作台的哪一栏，以及能收到哪个班的作业。')) return
  try {
    const r = await post<{ updated: number }>('/admin/users/class',
      { usernames: names, class_name: to })
    toast(`已改班 ${r.updated} 人`)
    selected.value = new Set()
    await load()
  } catch (e) {
    toast(detail(e, '改班失败'), 'error')
  }
}
</script>

<template>
  <section class="page">
    <CLPageHeader title="账号管理" sub="建号、批量导入名单、改角色与班级">
      <template #actions>
        <CLBadge variant="cite">{{ items.length }} 个账号</CLBadge>
        <CLBadge v-if="byRole.student">学生 {{ byRole.student }}</CLBadge>
      </template>
    </CLPageHeader>

    <CLCard title="批量导入名单" sub="CSV 首行是表头，必须有 username 列；其余 name / student_id / college / major / class_name / password 选填。">
      <template #header>
        <div>
          <div class="ctitle">批量导入名单</div>
          <div class="csub">
            CSV 首行是表头，<b>必须有 username 列</b>；其余
            <code>name / student_id / college / major / class_name / password</code> 选填。
          </div>
          <ul class="notes">
            <li><b>先预览再执行</b>。预览不写库，会逐行告诉你"将新建"还是"为什么跳过"。</li>
            <li><b>一律建成学生</b>。CSV 里写了 role 也不生效，助教和教务账号要单独创建。</li>
            <li><b>初始密码会显示在结果里</b>，顺序是：行内 password 列 → 下面的统一初始密码 →
              按学号兜底生成。系统没有邮件能力，<b>那一列是你把账号发给学生的唯一渠道</b>。</li>
            <li>一行出错不影响其他行：撞学号的那个跳过，其余照常建。</li>
          </ul>
        </div>
      </template>
      <div class="form">
        <div class="row">
          <div class="lbl">名单</div>
          <div class="filebox">
            <input ref="fileInput" class="hidden-input" type="file" accept=".csv" @change="onPicked" />
            <CLButton variant="ghost" @click="pick">选择 CSV…</CLButton>
            <span v-if="picked" class="picked">{{ picked.name }}</span>
            <span v-else class="picked muted">尚未选择</span>
          </div>
        </div>
        <div class="row">
          <div class="lbl">初始密码</div>
          <div class="filebox grow">
            <CLField v-model="defaultPassword" placeholder="留空则按学号生成（如 cl2021001）" />
          </div>
        </div>
        <div class="row">
          <div class="lbl"></div>
          <div class="filebox">
            <CLButton variant="ghost" :disabled="!picked || importing" @click="runImport(false)">
              {{ importing ? '处理中…' : '预览' }}
            </CLButton>
            <CLButton :disabled="!preview || preview.applied || !preview.created || importing"
              @click="runImport(true)">确认建号</CLButton>
          </div>
        </div>
      </div>

      <div v-if="preview" class="result">
        <div class="sum">
          <CLBadge :variant="preview.applied ? 'ok' : 'warn'">
            {{ preview.applied ? '已执行' : '预览，未写库' }}
          </CLBadge>
          <span>共 {{ preview.total }} 行 · {{ preview.applied ? '已建' : '将建' }}
            <b>{{ preview.created }}</b> · 跳过 <b>{{ preview.skipped }}</b></span>
        </div>
        <p v-if="preview.warning" class="warn-line">{{ preview.warning }}</p>
        <div class="tbl-wrap">
          <table class="tbl">
            <thead>
              <tr><th>行</th><th>用户名</th><th>姓名</th><th>班级</th><th>初始密码</th><th>结果</th></tr>
            </thead>
            <tbody>
              <tr v-for="r in preview.items" :key="r.line" :class="{ bad: !r.ok }">
                <td class="num">{{ r.line }}</td>
                <td class="fname">{{ r.username || '—' }}</td>
                <td>{{ r.name || '—' }}</td>
                <td>{{ r.class_name || '—' }}</td>
                <td><code v-if="r.ok && r.password">{{ r.password }}</code><span v-else class="muted">—</span></td>
                <td :class="r.ok ? 'ok-cell' : 'bad-cell'">{{ r.reason }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </CLCard>

    <CLCard title="单个建号" sub="助教与教务管理员只能在这里建；公网注册永远只能建学生。">
      <div class="form">
        <div class="grid">
          <CLField v-model="nu.username" label="用户名" placeholder="登录时使用" />
          <CLField v-model="nu.password" label="密码" type="password" placeholder="至少 8 位" />
          <CLField v-model="nu.name" label="姓名" placeholder="选填" />
          <CLField v-model="nu.student_id" label="学号" placeholder="选填，会查重" />
          <CLSelect v-model="nu.class_name" label="班级" :groups="classGroups" placeholder="选择或新建" />
        </div>
        <div class="row">
          <div class="lbl">角色</div>
          <CLSegmented v-model="nu.role"
            :options="Object.entries(ROLE_LABEL).map(([value, label]) => ({ label, value }))" />
        </div>
        <div class="row">
          <div class="lbl"></div>
          <CLButton :disabled="!nu.username || nu.password.length < 8 || creating" @click="create">
            {{ creating ? '建号中…' : '建号' }}
          </CLButton>
        </div>
      </div>
    </CLCard>

    <CLCard title="账号列表" sub="停用会让对方当前的登录状态立刻失效；学习记录一律保留。">
      <template #actions>
        <CLSegmented v-model="role" :options="ROLE_OPTIONS" />
        <CLSegmented v-model="status" :options="STATUS_OPTIONS" />
        <CLButton variant="ghost" @click="load">刷新</CLButton>
      </template>

      <div class="search">
        <CLField v-model="keyword" placeholder="搜用户名 / 姓名 / 学号" @enter="load" />
        <CLButton variant="ghost" @click="load">搜索</CLButton>
      </div>

      <div v-if="selected.size" class="batch">
        <span>已选 <b>{{ selected.size }}</b> 名学生</span>
        <CLSelect v-model="targetClass" :groups="classGroups" placeholder="改到哪个班（可新建）" />
        <CLButton :disabled="!targetClass.trim()" @click="applyClass">批量改班</CLButton>
        <CLButton variant="text" @click="selected = new Set()">取消选择</CLButton>
      </div>

      <CLSkeleton v-if="loading" :rows="5" />
      <CLEmpty v-else-if="!items.length" title="没有匹配的账号"
        hint="换个筛选条件，或用上面的名单导入建一批。" />
      <div v-else class="tbl-wrap">
        <table class="tbl">
          <thead>
            <tr>
              <th class="chk"><input type="checkbox" :checked="allChecked" @change="toggleAll" /></th>
              <th>用户名</th><th>姓名</th><th>学号</th><th>班级</th><th>角色</th><th>状态</th><th class="act">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="u in items" :key="u.username" :class="{ off: u.status === 'disabled' }">
              <td class="chk">
                <input v-if="u.role === 'student'" type="checkbox"
                  :checked="selected.has(u.username)" @change="toggleOne(u.username)" />
              </td>
              <td class="fname">{{ u.username }}</td>
              <td>{{ u.name || '—' }}</td>
              <td class="muted">{{ u.student_id || '—' }}</td>
              <td>{{ u.class_name || '—' }}</td>
              <td>
                <select class="rolesel" :value="u.role"
                  @change="setRole(u, ($event.target as HTMLSelectElement).value)">
                  <option v-for="(label, value) in ROLE_LABEL" :key="value" :value="value">{{ label }}</option>
                </select>
              </td>
              <td>
                <CLBadge :variant="u.status === 'active' ? 'ok' : 'neutral'">
                  {{ u.status === 'active' ? '启用中' : '已停用' }}
                </CLBadge>
              </td>
              <td class="act">
                <button class="plain" @click="toggleStatus(u)">
                  {{ u.status === 'active' ? '停用' : '启用' }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </CLCard>
  </section>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 18px; }

.form { display: flex; flex-direction: column; gap: 14px; }
.ctitle { font-size: 15px; font-weight: 700; color: var(--cl-text); }
.csub { margin-top: 4px; font-size: 13px; color: var(--cl-text-2); line-height: 1.6; }
.csub b, .notes b { color: var(--cl-text); font-weight: 700; }
.notes { margin: 8px 0 0; padding-left: 18px; display: flex; flex-direction: column; gap: 4px; }
.notes li { font-size: 12.5px; color: var(--cl-text-3); line-height: 1.6; }
.row { display: flex; align-items: center; gap: 14px; }
.lbl { width: 60px; flex-shrink: 0; font-size: 13px; font-weight: 700; color: var(--cl-text-3); }
.filebox { display: flex; align-items: center; gap: 12px; min-width: 0; }
.filebox.grow { flex: 1; max-width: 320px; }
.hidden-input { display: none; }
.picked { font-size: 13px; color: var(--cl-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.picked.muted { color: var(--cl-text-3); }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }
.search { display: flex; align-items: flex-end; gap: 10px; margin-bottom: 12px; max-width: 420px; }

/* 批量条只在有选中时出现：常驻会让人以为它总在等一个动作 */
.batch {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 10px 12px; margin-bottom: 12px;
  border: 1px solid var(--cl-border); border-radius: var(--cl-radius-sm);
  background: var(--cl-row-hover); font-size: 13px; color: var(--cl-text-2);
}
.batch b { color: var(--cl-text); }

.result { margin-top: 16px; }
.sum { display: flex; align-items: center; gap: 10px; font-size: 13px; color: var(--cl-text-2); margin-bottom: 8px; }
.sum b { color: var(--cl-text); }
.warn-line { margin: 0 0 8px; font-size: 12.5px; color: var(--cl-warn-fg); line-height: 1.6; }

.tbl-wrap { overflow-x: auto; }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th {
  text-align: left; font-weight: 700; font-size: 12px; white-space: nowrap;
  color: var(--cl-text-3); padding: 8px 10px; border-bottom: 1px solid var(--cl-row-border);
}
.tbl td { padding: 10px; border-bottom: 1px solid var(--cl-row-border); color: var(--cl-text-2); vertical-align: middle; }
.tbl tbody tr:hover { background: var(--cl-row-hover); }
.tbl .num { text-align: right; font-variant-numeric: tabular-nums; }
.tbl .muted { color: var(--cl-text-3); }
.tbl .chk { width: 34px; }
.fname { color: var(--cl-text); font-weight: 600; }
.tbl code {
  font-size: 12px; padding: 1px 6px; border-radius: var(--cl-radius-sm);
  background: var(--cl-row-hover); color: var(--cl-text);
}
/* 跳过的行不靠 hover 才显形：它是这张表上最需要被读到的东西 */
.tbl tr.bad td { background: var(--cl-err-bg); }
.ok-cell { color: var(--cl-ok-fg); }
.bad-cell { color: var(--cl-err-fg); font-weight: 600; }
/* 停用的账号整行压暗，但不隐藏 —— 教务要看得见"这个人被停了"，
   而不是找不到他之后以为号没建成 */
.tbl tr.off td { opacity: .55; }

.rolesel {
  font: inherit; font-size: 12px; padding: 4px 8px; cursor: pointer;
  border-radius: var(--cl-radius-sm); border: 1px solid var(--cl-border);
  background: transparent; color: var(--cl-text-2);
}
.act { text-align: right; white-space: nowrap; }
.plain {
  padding: 5px 12px; border-radius: var(--cl-radius-sm); cursor: pointer; font: inherit; font-size: 12px; font-weight: 600;
  background: transparent; border: 1px solid var(--cl-border); color: var(--cl-text-2);
  transition: color .12s ease, border-color .12s ease, background .12s ease;
}
.plain:hover { color: var(--cl-text); border-color: var(--cl-text-3); }
</style>
