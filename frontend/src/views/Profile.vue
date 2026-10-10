<script setup lang="ts">
import { computed, onActivated, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { get, put } from '@/api/client'
import { toast } from '@/api/toast'
import type { CLUser } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { COLLEGE_GROUPS } from '@/data/colleges'
import CLBadge from '@/components/ui/CLBadge.vue'
import CLButton from '@/components/ui/CLButton.vue'
import CLCard from '@/components/ui/CLCard.vue'
import CLField from '@/components/ui/CLField.vue'
import CLPageHeader from '@/components/ui/CLPageHeader.vue'
import CLProgress from '@/components/ui/CLProgress.vue'
import CLSelect from '@/components/ui/CLSelect.vue'
import CLSkeleton from '@/components/ui/CLSkeleton.vue'

const auth = useAuthStore()
const router = useRouter()
const loading = ref(true)
const saving = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const form = reactive({ name: '', college: '', major: '', class_name: '', email: '', phone: '', avatar: '' })

function fill(user: CLUser) {
  form.name = user.name || ''
  form.college = user.college || ''
  form.major = user.major || ''
  form.class_name = user.class_name || ''
  form.email = user.email || ''
  form.phone = user.phone || ''
  form.avatar = user.avatar || ''
}

async function load() {
  loading.value = true
  try {
    const result = await get<{ user: CLUser }>('/auth/profile')
    auth.setUser(result.user)
    fill(result.user)
  } finally { loading.value = false }
}

function imageFrom(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image()
    image.onload = () => resolve(image)
    image.onerror = reject
    image.src = url
  })
}

async function chooseAvatar(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  if (!file.type.startsWith('image/') || file.size > 8 * 1024 * 1024) {
    toast('请选择 8MB 以内的图片', 'error')
    return
  }
  const source = URL.createObjectURL(file)
  try {
    const image = await imageFrom(source)
    const side = Math.min(image.naturalWidth, image.naturalHeight)
    const sx = (image.naturalWidth - side) / 2
    const sy = (image.naturalHeight - side) / 2
    const canvas = document.createElement('canvas')
    canvas.width = 320; canvas.height = 320
    canvas.getContext('2d')?.drawImage(image, sx, sy, side, side, 0, 0, 320, 320)
    form.avatar = canvas.toDataURL('image/jpeg', 0.84)
  } catch {
    toast('头像读取失败，请换一张图片', 'error')
  } finally {
    URL.revokeObjectURL(source)
    if (fileInput.value) fileInput.value.value = ''
  }
}

async function save() {
  if (saving.value) return
  if (form.email.trim() && !form.email.includes('@')) {
    toast('邮箱格式不正确', 'error'); return
  }
  saving.value = true
  try {
    const result = await put<{ ok: boolean; user: CLUser }>('/auth/profile', form)
    auth.setUser(result.user)
    fill(result.user)
    toast('个人信息已保存')
  } catch (error) {
    const detail = (error as { response?: { data?: { detail?: string } } }).response?.data?.detail
    toast(detail || '保存失败，请稍后重试', 'error')
  } finally { saving.value = false }
}

// 资料完整度：给填写进度一个直观反馈，而不是让"基本资料"看起来只是一张裸表单
const COMPLETENESS_FIELDS = ['avatar', 'college', 'major', 'class_name', 'email', 'phone'] as const
const completeness = computed(() => {
  const filled = COMPLETENESS_FIELDS.filter((key) => form[key].trim()).length
  return Math.round((filled / COMPLETENESS_FIELDS.length) * 100)
})

// 版本戳不再上屏：交付产品不对用户暴露提交号和部署状态。
// 前后端版本核对改用 scripts/preflight_demo.py（读 dist/build.json + 管理员接口 /admin/runtime）。
onActivated(load)
</script>

<template>
  <div class="profile-page">
    <CLPageHeader title="个人信息" sub="头像与基本资料">
      <template #actions><CLButton variant="ghost" @click="router.push({ name: 'center' })">查看学习中心</CLButton></template>
    </CLPageHeader>

    <CLSkeleton v-if="loading" :rows="6" />
    <div v-else class="profile-stack">
      <CLCard class="hero">
        <div class="hero-avatar">
          <button class="avatar-picker" type="button" @click="fileInput?.click()">
            <img v-if="form.avatar" :src="form.avatar" alt="当前头像" />
            <span v-else>{{ auth.displayName.slice(0, 1) }}</span>
            <i><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h3l1.6-2h6.8L17 7h3a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1Z" /><circle cx="12" cy="13" r="3.6" /></svg>换头像</i>
          </button>
          <input ref="fileInput" class="file-input" type="file" accept="image/jpeg,image/png,image/webp" @change="chooseAvatar" />
          <button v-if="form.avatar" type="button" class="avatar-remove" @click="form.avatar = ''">移除头像</button>
        </div>

        <div class="hero-body">
          <div class="hero-top">
            <h2 class="hero-name">{{ form.name || auth.user?.username }}</h2>
            <CLBadge variant="cite">{{ auth.roleLabel }}</CLBadge>
          </div>
          <div class="hero-meta">
            <span>@{{ auth.user?.username }}</span>
            <span v-if="auth.user?.student_id">学号 {{ auth.user?.student_id }}</span>
            <span v-if="form.college">{{ form.college }}</span>
          </div>
          <CLProgress class="hero-progress" :value="completeness" label="资料完整度" :right="`${completeness}%`" />
        </div>
      </CLCard>

      <CLCard title="基本资料" sub="保存后侧边栏与学习中心会立即同步">
        <div class="form-grid">
          <CLField v-model="form.name" label="姓名 / 昵称" placeholder="请输入姓名" />
          <CLSelect v-model="form.college" label="学院" placeholder="选择所在学院" :groups="COLLEGE_GROUPS" />
          <CLField v-model="form.major" label="专业" placeholder="如 计算机科学与技术" />
          <CLField v-model="form.class_name" label="班级" placeholder="如 软件工程1班" />
          <CLField v-model="form.email" label="邮箱" placeholder="name@example.com" />
          <CLField v-model="form.phone" label="手机" placeholder="用于联系或找回账号" />
        </div>
        <div class="foot">
          <p class="immutable">用户名、学号和角色属于身份字段，如需变更请联系管理员。</p>
          <CLButton :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存修改' }}</CLButton>
        </div>
      </CLCard>
    </div>
  </div>
</template>

<style scoped>
.profile-page { max-width: 820px; margin: 0 auto; }
.profile-stack { display: flex; flex-direction: column; gap: 18px; }

/* 与 Center 的主推卡同一配方：深靛蓝玻璃 + 右上角局部光。
   Hero 是这一页唯一的"身份展示区"，其余都是表单，靠它撑住页面重心。 */
/* 同 Center 的 .focus.clcard：多带一个类名压过 CLCard 的 .clcard，
   不依赖样式注入顺序。 */
.hero.clcard {
  position: relative;
  display: flex; align-items: center; gap: 26px;
  background: var(--cl-accent-surface);
  border-color: var(--cl-tint-border);
  overflow: hidden;
}
.hero::after {
  content: '';
  position: absolute; right: -40px; top: -120px;
  width: 300px; height: 300px; border-radius: 50%;
  background: var(--cl-accent-halo);
  filter: blur(26px); pointer-events: none;
}
.hero-avatar { position: relative; display: flex; flex-direction: column; align-items: center; gap: 8px; flex-shrink: 0; }
/* 光环用半透明靛蓝而不是 --cl-surface 的实色描边 ——
   实色环压在玻璃卡面上会变成一个看得见的灰盘子，把头像"钉"在一块补丁上。 */
.avatar-picker {
  position: relative; width: 108px; height: 108px; border-radius: 50%; overflow: hidden;
  padding: 0; cursor: pointer; color: white; font: inherit; font-size: 38px; font-weight: 800;
  background: linear-gradient(135deg, var(--cl-brand), var(--cl-accent));
  box-shadow: 0 0 0 4px var(--cl-tint-border), var(--cl-shadow-md);
  transition: transform .16s ease, box-shadow .16s ease;
}
.avatar-picker:hover, .avatar-picker:focus-visible {
  transform: translateY(-2px);
  box-shadow: 0 0 0 4px var(--cl-tint-border-strong), var(--cl-glow);
}
.avatar-picker img { width: 100%; height: 100%; object-fit: cover; display: block; }
/* 「换头像」蒙层：默认收在圆形下方，hover 才滑上来。
   底色压到接近不透明 —— 头像本身是用户上传的任意图片，半透明蒙层上的白字
   遇到浅色照片会读不出来。 */
.avatar-picker i {
  position: absolute; inset: auto 0 0; display: flex; align-items: center; justify-content: center; gap: 4px;
  padding: 7px 3px; background: var(--cl-inset-deep); backdrop-filter: blur(3px);
  color: white; font-size: 11px; font-style: normal; font-weight: 600;
  transform: translateY(100%); transition: transform .16s ease;
}
.avatar-picker i svg { width: 13px; height: 13px; fill: none; stroke: currentColor; stroke-width: 1.8; }
.avatar-picker:hover i, .avatar-picker:focus-visible i { transform: translateY(0); }
.file-input { display: none; }
.avatar-remove { border: 0; background: none; padding: 2px 6px; border-radius: 6px; font: inherit; font-size: 12px; color: var(--cl-text-3); cursor: pointer; }
.avatar-remove:hover { color: var(--cl-err); background: var(--cl-err-tint); }

.hero-body { position: relative; flex: 1; min-width: 0; }
.hero-top { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.hero-name {
  margin: 0; font-size: 20px; font-weight: 800; letter-spacing: .2px;
  background: var(--cl-text-grad);
  -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent;
}
/* 用户名/学号/学院三段用竖线隔开，比只靠间距更清楚它们是并列的三个字段 */
.hero-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; margin: 7px 0 16px; font-size: 13px; color: var(--cl-text-2); }
.hero-meta span + span { padding-left: 12px; border-left: 1px solid var(--cl-row-border); }
.hero-progress { max-width: 320px; }

.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.foot {
  display: flex; justify-content: space-between; align-items: center; gap: 14px;
  margin-top: 20px; padding-top: 16px; border-top: 1px solid var(--cl-row-border);
}
.immutable { margin: 0; font-size: 12px; line-height: 1.6; color: var(--cl-text-3); }
@media (max-width: 620px) {
  .form-grid { grid-template-columns: 1fr; }
  .hero { flex-direction: column; text-align: center; }
  .hero-meta { justify-content: center; }
  .hero-progress { max-width: none; }
  .foot { flex-direction: column; align-items: stretch; }
}
</style>
