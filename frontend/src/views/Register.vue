<script setup lang="ts">
// 注册页。与登录页共用 CLAuthShell 整页外壳，卡片换成注册表单（wide 宽卡）。
//
// 字段取舍：用户名/密码是账号本身，姓名/学号是身份，学院/专业/班级/邮箱/手机
// 是助教侧看班级学情时的分组依据 —— 除前四项外全部选填，注册这一步卡得越少越好。
//
// 角色不给选：公网注册一律落成学生。助教能看到全班错题与班级统计，
// 那是要人工开的号（后端 RegisterReq 里也已经没有 role 字段了）。
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { get, post } from '@/api/client'
import { toast } from '@/api/toast'
import type { CLUser } from '@/api/types'
import { COLLEGE_GROUPS } from '@/data/colleges'
import CLLogo from '@/components/ui/CLLogo.vue'
import CLButton from '@/components/ui/CLButton.vue'
import CLField from '@/components/ui/CLField.vue'
import CLSelect from '@/components/ui/CLSelect.vue'
import CLAuthShell from '@/components/ui/CLAuthShell.vue'

const router = useRouter()
const auth = useAuthStore()

const name = ref('')
const studentId = ref('')
const username = ref('')
const pw1 = ref('')
const pw2 = ref('')
const college = ref('')
const major = ref('')
const className = ref('')
// 已有班级。班级至今是自由文本，"计科2101" / "计科 2101" / "计算机2101" 会变成
// 三个班，而助教工作台的热力图与作业下发按它精确匹配分组 —— 一个错字就让这个人
// 从此不在他该在的那一栏里。让能挑就挑（allowCreate 默认开着，第一个人仍能新建），
// 是最便宜的一道防线。取不到就退回纯输入，不挡注册。
const classes = ref<string[]>([])
const classGroups = computed(() => (classes.value.length
  ? [{ label: '已有班级', options: classes.value }]
  : []))
const email = ref('')
const phone = ref('')
const captchaIn = ref('')
const busy = ref(false)

// 算术验证码：注册页是公网上唯一能匿名写库的入口，挡一挡最粗浅的脚本注册。
const ca = ref(0)
const cb = ref(0)
function newCaptcha() {
  ca.value = 1 + Math.floor(Math.random() * 9)
  cb.value = 1 + Math.floor(Math.random() * 9)
  captchaIn.value = ''
}
onMounted(async () => {
  newCaptcha()
  // 取不到班级列表不挡注册：CLSelect 的 allowCreate 让它退化成一个普通输入框。
  classes.value = (await get<{ items: string[] }>('/classes').catch(() => ({ items: [] }))).items
})

function invalid(): string {
  if (!name.value.trim()) return '请填写姓名'
  if (!username.value.trim()) return '请填写用户名'
  if (!pw1.value) return '请填写密码'
  if (pw1.value.length < 6) return '密码至少 6 位'
  if (pw1.value !== pw2.value) return '两次输入的密码不一致'
  if (email.value.trim() && !email.value.includes('@')) return '邮箱格式不对'
  if (captchaIn.value.trim() !== String(ca.value + cb.value)) return '验证码算错了'
  return ''
}

async function submit() {
  if (busy.value) return
  const err = invalid()
  if (err) {
    toast(err, 'error')
    // 验证码答错就换一道，否则等于没换
    if (err === '验证码算错了') newCaptcha()
    return
  }
  busy.value = true
  try {
    const r = await post<{ ok: boolean; user: CLUser; token: string }>('/auth/register', {
      username: username.value.trim(),
      password: pw1.value,
      name: name.value.trim(),
      student_id: studentId.value.trim(),
      college: college.value.trim(),
      major: major.value.trim(),
      class_name: className.value.trim(),
      email: email.value.trim(),
      phone: phone.value.trim(),
    })
    // 注册成功即登录：让人刚填完十个字段又去登录一次是没必要的摩擦。
    // 后端在注册响应里一并签发令牌，这里直接存成会话。
    auth.setSession(r.user, r.token)
    toast(`欢迎，${r.user.name || r.user.username}`)
    router.replace({ name: 'center' })
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    toast(detail || '注册失败，请稍后再试', 'error')
    newCaptcha()
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <CLAuthShell wide>
    <template #logo>
      <CLLogo :size="26" tone="scene" />
      <span class="wordmark">CourseLoop</span>
    </template>

    <!-- 字段密度靠这层的自定义属性统一收窄，不逐个 :deep 覆盖 -->
    <div class="dense">
      <div class="head">
        <h2>创建账号</h2>
        <p>只有前四项必填，其余可以之后再补</p>
      </div>

      <div class="row">
        <CLField v-model="name" label="姓名" placeholder="请输入姓名" />
        <CLField v-model="studentId" label="学号（选填）" placeholder="用于助教核对身份" />
      </div>

      <CLField v-model="username" label="用户名" placeholder="登录时使用，支持中文" class="one" />

      <div class="row">
        <CLField v-model="pw1" label="密码" type="password" placeholder="至少 6 位" />
        <CLField v-model="pw2" label="确认密码" type="password" placeholder="再输一次" @enter="submit" />
      </div>

      <!-- 选填字段收进可折叠区：必填的四项之下再压六个输入框，注册意愿会被劝退。
           默认展开，想快速过的人可以直接收起。 -->
      <details class="more" open>
        <summary>
          <span class="sum-label">补充信息</span>
          <span class="sum-hint">选填 · 助教按学院/班级看学情时会用到</span>
          <svg class="sum-caret" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6" /></svg>
        </summary>

        <div class="more-body">
          <CLSelect
            v-model="college" label="学院" placeholder="选择所在学院"
            :groups="COLLEGE_GROUPS" class="one"
          />

          <div class="row">
            <CLField v-model="major" label="专业" placeholder="如 计算机科学与技术" />
            <CLSelect v-model="className" label="班级" placeholder="选择或填写班级"
              :groups="classGroups" />
          </div>

          <div class="row">
            <CLField v-model="email" label="邮箱" placeholder="name@example.com" />
            <CLField v-model="phone" label="手机" placeholder="用于找回密码" />
          </div>
        </div>
      </details>

      <div class="captcha">
        <span class="cap-q" aria-hidden="true">{{ ca }} + {{ cb }} =</span>
        <CLField v-model="captchaIn" label="验证码" placeholder="填写计算结果" @enter="submit" />
      </div>

      <CLButton variant="primary" block :disabled="busy" class="submit" @click="submit">
        <span class="btn-label">{{ busy ? '注册中' : '注册' }}</span>
      </CLButton>

      <p class="to-login">
        已有账号？<router-link :to="{ name: 'login' }">返回登录</router-link>
      </p>
    </div>
  </CLAuthShell>
</template>

<style scoped>
/* 控件本身的深色玻璃皮在 CLAuthShell 的非 scoped 样式里（.cl-auth 前缀），
   这里只写注册页的字段排布与折叠区。 */

.head { margin-bottom: 22px; animation: item-in .5s ease both .2s; }
.head h2 {
  font-size: 25px; font-weight: 800; margin: 0 0 6px; letter-spacing: .4px;
  background: var(--cl-text-grad);
  -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent;
}
.head p { margin: 0; font-size: 13px; color: var(--cl-text-2); }

/* 短字段两两并排：十个字段平铺会长到一屏放不下，成对压缩后正好收在视口内 */
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }
.one { display: block; margin-bottom: 12px; }

/* 字段密集的长表单，控件比登录页收一号，否则整卡显得笨重。
   只重设外壳约定的变量：继承下去就生效，不必和外壳的规则比权重。 */
.dense {
  --af-h: 44px;
  --af-fs: 13.5px;
  --af-pl: 13px;
  --af-pr: 13px;
  --af-label-fs: 11.5px;
  --af-label-mb: 6px;
}

/* ---------- 折叠的选填区 ---------- */
.more {
  margin: 4px 0 14px;
  border: 1px solid var(--cl-glass-border);
  border-radius: 14px;
  background: var(--cl-inset);
}
.more > summary {
  display: flex; align-items: center; gap: 10px;
  padding: 12px 14px; cursor: pointer; list-style: none;
  font-size: 13px; font-weight: 700; color: var(--cl-text);
}
/* Safari 还认 -webkit-details-marker，不去掉会多一个三角 */
.more > summary::-webkit-details-marker { display: none; }
.more > summary:hover { color: var(--cl-text); }
.sum-hint { flex: 1; font-size: 11.5px; font-weight: 500; color: var(--cl-text-3); }
.sum-caret {
  width: 15px; height: 15px; flex: none; fill: none;
  stroke: var(--cl-auth-rule); stroke-width: 2.2; stroke-linecap: round; stroke-linejoin: round;
  transition: transform .2s ease;
}
.more[open] > summary .sum-caret { transform: rotate(180deg); }
.more-body { padding: 2px 14px 4px; }
/* 折叠区最后一个字段的下边距由容器 padding 接管，否则底部空一大块 */
.more-body > :last-child { margin-bottom: 8px; }

/* ---------- 验证码 ---------- */
/* 算式单独做成一块，比塞进 label 里读得清楚 */
.captcha { display: grid; grid-template-columns: auto 1fr; gap: 12px; align-items: end; }
.cap-q {
  height: 44px; display: grid; place-items: center; padding: 0 16px;
  border: 1px solid var(--cl-glass-border); border-radius: 13px;
  background: var(--cl-tint);
  font-family: var(--cl-mono); font-size: 15px; font-weight: 700;
  color: var(--cl-accent-soft); letter-spacing: 1px; white-space: nowrap; user-select: none;
}

/* ---------- 按钮 / 尾部 ---------- */
.submit { margin-top: 18px; }
.btn-label { position: relative; z-index: 1; }

.to-login { margin: 16px 0 0; text-align: center; font-size: 12.5px; color: var(--cl-text-2); }
.to-login a {
  font-weight: 700; color: var(--cl-brand-600);
  border-bottom: 1px solid var(--cl-tint-border-strong); padding-bottom: 1px;
  transition: color .15s ease, border-color .15s ease;
}
.to-login a:hover { color: var(--cl-accent); border-color: var(--cl-accent); text-decoration: none; }

@keyframes item-in {
  from { opacity: 0; transform: translateY(10px); }
  to   { opacity: 1; transform: none; }
}
@media (prefers-reduced-motion: reduce) {
  .head { animation: none !important; }
}

@media (max-width: 880px) {
  .head h2 { font-size: 22px; }
}
/* 更窄时并排的两列会挤到看不清标签，退回单列 */
@media (max-width: 520px) {
  .row { grid-template-columns: 1fr; gap: 0; margin-bottom: 0; }
  .row > * { margin-bottom: 12px; }
  .captcha { grid-template-columns: 1fr; align-items: stretch; gap: 8px; }
  .cap-q { justify-content: start; place-items: center start; }
}
</style>
