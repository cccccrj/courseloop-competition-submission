<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { homeRouteName } from '@/router'
import { useAuthStore } from '@/stores/auth'
import { post } from '@/api/client'
import { toast } from '@/api/toast'
import type { CLUser } from '@/api/types'
import CLLogo from '@/components/ui/CLLogo.vue'
import CLButton from '@/components/ui/CLButton.vue'
import CLField from '@/components/ui/CLField.vue'
import CLSegmented from '@/components/ui/CLSegmented.vue'
import CLAuthShell from '@/components/ui/CLAuthShell.vue'

const roleOptions = [
  { label: '学生', value: 'student' },
  { label: '助教', value: 'ta' },
]

const router = useRouter()
const auth = useAuthStore()
const username = ref('')
const password = ref('')
const role = ref('student')
const busy = ref(false)
// 明文切换：密码填错重来一次的成本，比"能被人看一眼"的风险高得多
const showPw = ref(false)

async function enter() {
  if (busy.value) return
  busy.value = true
  try {
    const uname = username.value.trim()
    const pwd = password.value
    if (!uname || !pwd) {
      toast('请输入用户名和密码', 'error')
      return
    }
    try {
      const r = await post<{ ok: boolean; user: CLUser; token: string }>(
        '/auth/login', { username: uname, password: pwd })
      auth.setSession(r.user, r.token)
    } catch (e) {
      // 骨架期这里有一条「后端连不上就本地登录」的兜底，现在删掉了：
      // 身份改成后端签名的令牌之后，拿不到令牌就等于没有身份，本地登录进去
      // 也是处处 401 —— 与其让人进去碰一鼻子灰，不如在这里说清楚。
      const err = e as { response?: { data?: { detail?: string } } }
      toast(err.response?.data?.detail || '登录失败，请检查用户名和密码，或稍后再试', 'error')
      return
    }
    // 角色从 store 读而不是从响应里读：setSession 已经把它落好了，而 r 只活在
    // try 块里。两者同源，用 store 还顺带保证了"跳转依据"和"守卫依据"是同一份。
    router.replace({ name: homeRouteName(auth.user?.role) })
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <!-- 版式、氛围层、卡片外框都在 CLAuthShell 里，两页共用，切换时不会"跳一下" -->
  <CLAuthShell>
    <template #logo>
      <CLLogo :size="26" tone="scene" />
      <span class="wordmark">CourseLoop</span>
    </template>

    <div class="head">
      <h2>欢迎回来</h2>
      <p>登录以继续你的学习</p>
    </div>

    <CLSegmented v-model="role" :options="roleOptions" class="seg" />

    <div class="fw fw-user">
      <span class="ic" aria-hidden="true">
        <svg viewBox="0 0 24 24">
          <circle cx="12" cy="8" r="3.6" />
          <path d="M4.8 20c0-3.6 3.2-5.6 7.2-5.6s7.2 2 7.2 5.6" />
        </svg>
      </span>
      <CLField v-model="username" label="用户名" placeholder="请输入用户名" @enter="enter" />
    </div>

    <div class="fw fw-pw">
      <span class="ic" aria-hidden="true">
        <svg viewBox="0 0 24 24">
          <rect x="4.5" y="10.5" width="15" height="9.5" rx="2.4" />
          <path d="M8.2 10.5V7.8a3.8 3.8 0 0 1 7.6 0v2.7" />
        </svg>
      </span>
      <CLField
        v-model="password" label="密码" :type="showPw ? 'text' : 'password'"
        placeholder="请输入密码" @enter="enter"
      />
      <button
        class="eye" type="button"
        :aria-label="showPw ? '隐藏密码' : '显示密码'" @click="showPw = !showPw"
      >
        <svg v-if="showPw" viewBox="0 0 24 24">
          <path d="M3 3l18 18" />
          <path d="M10.6 6.3A8.6 8.6 0 0 1 12 6.2c5 0 9 5.8 9 5.8a17 17 0 0 1-2.7 3.2M6.4 8.1A16.8 16.8 0 0 0 3 12s4 5.8 9 5.8c1 0 1.9-.2 2.7-.5" />
          <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2" />
        </svg>
        <svg v-else viewBox="0 0 24 24">
          <path d="M3 12s4-5.8 9-5.8S21 12 21 12s-4 5.8-9 5.8S3 12 3 12z" />
          <circle cx="12" cy="12" r="2.8" />
        </svg>
      </button>
    </div>

    <CLButton variant="primary" block :disabled="busy" class="submit" @click="enter">
      <span class="btn-label">{{ busy ? '登录中' : '登录' }}</span>
    </CLButton>

    <p class="to-reg">
      还没有账号？<router-link :to="{ name: 'register' }">立即注册</router-link>
    </p>
  </CLAuthShell>
</template>

<style scoped>
/* 控件本身的深色玻璃皮在 CLAuthShell 的非 scoped 样式里（.cl-auth 前缀），
   这里只写登录页自己的排布、图标和入场节奏。 */

/* 卡片内元素依次入场，节奏比整块一起出现更"贵" */
/* 入场压到 0.6s 内（第二阶段 · 质感）：原先逐项错开到 1.08s 才出齐，
   截图时一秒内是一片空白，第一眼像"没加载出来"。错落感保留，只是更快。 */
.head    { animation: item-in .3s ease both .06s; }
.seg     { animation: item-in .3s ease both .1s; }
.fw-user { animation: item-in .3s ease both .14s; }
.fw-pw   { animation: item-in .3s ease both .18s; }
.submit  { animation: item-in .3s ease both .22s; }
.to-reg  { animation: item-in .3s ease both .28s; }
@keyframes item-in {
  from { opacity: 0; transform: translateY(10px); }
  to   { opacity: 1; transform: none; }
}

.head { margin-bottom: 24px; }
.head h2 {
  font-size: 26px; font-weight: 800; margin: 0 0 7px; letter-spacing: .4px;
  background: var(--cl-text-grad);
  -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent;
}
.head p { margin: 0; font-size: 13.5px; color: var(--cl-text-2); }

.seg { margin-bottom: 20px; }

/* ---------- 字段 ---------- */
/* 图标按输入框 50px 的高度绝对定位；外壳里改了 .clf-input 的 height 就要同步这里 */
.fw { position: relative; margin-bottom: 14px; }
.ic {
  position: absolute; left: 15px; bottom: 0; height: 50px;
  display: grid; place-items: center; pointer-events: none; z-index: 1;
}
.ic svg { width: 17px; height: 17px; fill: none; stroke: var(--cl-text-3); stroke-width: 1.7; stroke-linecap: round; }
.fw:focus-within .ic svg { stroke: var(--cl-brand); }
/* 图标和眼睛按钮占掉左右两侧，输入文字要让位。改变量而不是写 :deep 覆盖 ——
   :deep 算出来的权重和外壳里的规则同档，谁生效会取决于打包顺序。 */
.fw    { --af-pl: 43px; }
.fw-pw { --af-pr: 46px; }

.eye {
  position: absolute; right: 5px; bottom: 0; height: 50px; width: 42px;
  display: grid; place-items: center;
  border: none; background: transparent; cursor: pointer; z-index: 1;
  border-radius: 10px; transition: background .15s ease;
}
.eye svg { width: 17px; height: 17px; fill: none; stroke: var(--cl-text-3); stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
.eye:hover svg { stroke: var(--cl-brand-700); }
.eye:hover { background: var(--cl-tint); }

/* ---------- 按钮 / 尾部 ---------- */
.submit { margin-top: 6px; }
.btn-label { position: relative; z-index: 1; }

.to-reg { margin: 20px 0 0; text-align: center; font-size: 13px; color: var(--cl-text-2); }
.to-reg a {
  font-weight: 700; color: var(--cl-brand-600);
  border-bottom: 1px solid var(--cl-tint-border-strong); padding-bottom: 1px;
  transition: color .15s ease, border-color .15s ease;
}
.to-reg a:hover { color: var(--cl-accent); border-color: var(--cl-accent); text-decoration: none; }

@media (prefers-reduced-motion: reduce) {
  .head, .seg, .fw, .submit, .to-reg { animation: none !important; }
}

@media (max-width: 880px) {
  .head h2 { font-size: 23px; }
  .head { margin-bottom: 20px; }
}
</style>
