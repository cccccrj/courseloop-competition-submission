<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useCourseStore } from '@/stores/course'
import { useChatStore } from '@/stores/chat'
import { useSolveStore } from '@/stores/solve'
import { useExamStore } from '@/stores/exam'
import CLLogo from '@/components/ui/CLLogo.vue'
import CLCourseSwitch from '@/components/ui/CLCourseSwitch.vue'
import CLThemeSwitch from '@/components/ui/CLThemeSwitch.vue'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const courseStore = useCourseStore()
const chat = useChatStore()
const solve = useSolveStore()
const exam = useExamStore()

onMounted(() => courseStore.load())

const all = computed(() =>
  router.getRoutes().filter((r) => r.meta?.title && !r.meta?.hideNav),
)

/** 折叠段的段名。段里装的是路由表里标了 `fold` 的③④两段（阶段性回看 + 辅助旁支）。 */
const FOLD_LABEL = '进阶与回看'
/** 助教 / 教师 / 教务看到的学生入口统一收进这一段（测试报告 I-07）。 */
const STAFF_STUDENT_LABEL = '学生视角（体验）'

// 不是学生的人（助教、教师、教务）。他们也能打开学生页面 —— 试做题、看学生看到的
// 是什么 —— 但那不是他们的本职。原先侧栏把十二个学生入口和"助教工作台"并排摆着，
// 分段名只叫"学习"，看不出这些是"学生的功能"，也看不出在里面做题算谁的。
const isStaff = computed(() => !!auth.user && auth.user.role !== 'student')
/** 学生功能页：路由没有 roles 限制、也不是隐藏页（个人信息）。 */
const isStudentPage = computed(() => !route.meta?.roles && !route.meta?.hideNav && !!route.meta?.title)

interface NavItem { name: string; title: string }
interface NavSection { label: string; items: NavItem[]; fold: boolean; roleOnly: boolean }

// 分组名取自路由的 meta.group（缺省「学习」），而不是在这里写死。
// 原先是「无 roles 的归学习，有 roles 的归助教」—— 加了教务的页面之后，
// 那个二分法会把资料入库塞进「助教」组里。分组出现第三种时，判据就得
// 由路由自己声明。段内顺序沿用路由表顺序。
//
// 段的先后原先是"按首次出现"，也就是路由表顺序，于是助教工作台排在十二个
// 学习条目之后 —— 而那个顺序是按**学生**的使用频率定的，助教每天第一个点的
// 东西被排到了最底下，屏幕矮一点就在视口外（见 docs/32）。现在按段的性质排：
// 角色段置顶（它只对这个角色可见，本身就是"你是谁"的答案），常驻段居中，
// 折叠段垫底。
const groups = computed<NavSection[]>(() => {
  const role = auth.user!.role
  const sections: NavSection[] = []
  for (const r of all.value) {
    const roles = r.meta!.roles as string[] | undefined
    if (roles && !roles.includes(role)) continue
    // 非学生角色：所有学生入口（含原本常驻的那几项）合成一个默认折叠的"学生视角"段
    const staffStudent = isStaff.value && !roles
    const fold = staffStudent || Boolean(r.meta!.fold)
    const label = staffStudent ? STAFF_STUDENT_LABEL
      : fold ? FOLD_LABEL : ((r.meta!.group as string) || '学习')
    let sec = sections.find((x) => x.label === label)
    if (!sec) sections.push((sec = { label, items: [], fold, roleOnly: true }))
    // 整段都由 roles 限定才算角色段 —— 用「有没有 group」判会把将来任何一个
    // 新分组都误当成角色段
    if (!roles) sec.roleOnly = false
    sec.items.push({ name: r.name as string, title: r.meta!.title as string })
  }
  const rank = (s: NavSection) => (s.fold ? 2 : s.roleOnly ? 0 : 1)
  // sort 是稳定的，同档内仍是路由表顺序
  return sections.sort((a, b) => rank(a) - rank(b))
})

/* ---------- 折叠段 ---------- */
const FOLD_KEY = 'cl_nav_fold'

function readFold(): boolean {
  try {
    return localStorage.getItem(FOLD_KEY) === '1'
  } catch {
    return false           // 隐私模式：收起来就是默认态，不该因此报错
  }
}

const foldOpen = ref(readFold())

function toggleFold() {
  foldOpen.value = !foldOpen.value
  try { localStorage.setItem(FOLD_KEY, foldOpen.value ? '1' : '0') } catch { /* 同上 */ }
}

// 人已经在折叠段里的某一页时，把那段展开。不这么做的话，从别处跳进真题模考，
// 侧栏上没有任何一项是选中的 —— 看着像是"我在的这一页不在导航里"。
// 这一次展开不落盘：它是"让你看见自己在哪"，不是一次偏好设置。
watch(() => route.name, () => {
  if (route.meta?.fold || (isStaff.value && isStudentPage.value)) foldOpen.value = true
}, { immediate: true })

// 顶栏只报"在哪一段"，不再重复页面名：页面自己的大标题就在正下方，
// 两处写同一个"学习大厅"是视觉走查里最先被挑出来的重复（第二阶段 · 全站规范）。
const currentSection = computed(() => {
  if (route.meta?.hideNav) return '账户'
  if (isStaff.value && isStudentPage.value) return '学生视角'
  if (route.meta?.fold) return FOLD_LABEL
  return (route.meta?.group as string) || '学习'
})

/* ---------- 窄屏抽屉 ---------- */
// ≤768px 时侧栏不再占一列，改成从左侧滑出的抽屉（样式见底部 media 段）。
// 宽屏下这个状态没有任何效果，所以不需要监听宽度变化。
// 换页即收起：抽屉里点完导航，人要看的是新页面，不是还盖在上面的菜单。
const navOpen = ref(false)
watch(() => route.fullPath, () => { navOpen.value = false })
function onKey(e: KeyboardEvent) { if (e.key === 'Escape') navOpen.value = false }
onMounted(() => window.addEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))

function logout() {
  // 会话状态现在活得比页面久（store + sessionStorage），所以退出必须显式清 ——
  // 否则同一台机器上的下一个人打开大厅，看到的是上一个人的对话。
  // 库里的记录不动：那是本人的学习记录，重新登录还要读回来。
  chat.clearAll()
  solve.clearAll()
  exam.clearAll()
  auth.logout()
  router.replace({ name: 'login' })
}
</script>

<template>
  <div class="shell">
    <!-- 氛围层。三点必须同时成立，缺一个就会出问题：
         1. fixed 而非 absolute —— .content 是滚动容器，跟着滚的光晕会被拖出重影；
         2. 铺在 .shell 这一层而不是 .content 里面 —— 侧栏和顶栏也要坐在同一片
            氛围上，否则左右两半像两张拼起来的图；
         3. pointer-events:none —— 它盖在整个视口上，不关掉就把全站点击吃了。
         网格底纹不在这里，它留在 .content 的背景里跟着内容滚，
         "静止的光 + 滚动的网格"才有纵深，两个都固定就是一张贴纸。 -->
    <div class="bg" aria-hidden="true">
      <span class="glow g1"></span>
      <span class="glow g2"></span>
      <div class="grain"></div>
      <div class="vignette"></div>
    </div>

    <div class="nav-backdrop" :class="{ show: navOpen }" aria-hidden="true" @click="navOpen = false"></div>

    <aside id="cl-sidebar" class="sidebar" :class="{ open: navOpen }">
      <router-link class="brand" :to="{ name: 'center' }">
        <CLLogo :size="26" tone="nav" />
        <div class="brand-text">
          <div class="brand-name">CourseLoop</div>
          <div class="brand-sub">专业课学习助手</div>
        </div>
      </router-link>

      <nav class="nav">
        <div v-for="sec in groups" :key="sec.label" class="nav-group">
          <!-- 折叠段的段名是个按钮，其余段仍是一行静态标题 -->
          <button v-if="sec.fold" type="button" class="nav-label fold-toggle"
            :aria-expanded="foldOpen" @click="toggleFold">
            <span class="fold-name">{{ sec.label }}</span>
            <span v-if="!foldOpen" class="fold-count">{{ sec.items.length }}</span>
            <span class="chev" :class="{ open: foldOpen }" aria-hidden="true">›</span>
          </button>
          <div v-else class="nav-label">{{ sec.label }}</div>

          <template v-if="!sec.fold || foldOpen">
            <router-link
              v-for="item in sec.items"
              :key="item.name"
              :to="{ name: item.name }"
              class="nav-item"
              active-class="active"
            >
              <span class="bar"></span>{{ item.title }}
            </router-link>
          </template>
        </div>
      </nav>

      <div class="user-box" role="button" tabindex="0" title="进入个人信息"
        @click="router.push({ name: 'profile' })"
        @keydown.enter="router.push({ name: 'profile' })"
        @keydown.space.prevent="router.push({ name: 'profile' })">
        <div class="user-link">
          <div class="avatar"><img v-if="auth.user?.avatar" :src="auth.user.avatar" alt="" /><span v-else>{{ auth.displayName.slice(0, 1) }}</span></div>
          <div class="user-meta">
            <div class="user-name">{{ auth.displayName }}</div>
            <div class="user-role">{{ auth.roleLabel }}</div>
          </div>
          <span class="enter" aria-hidden="true">›</span>
        </div>
        <button class="logout" title="退出" @click.stop="logout">退出</button>
      </div>
    </aside>

    <div class="main">
      <header class="topbar">
        <button type="button" class="menu-btn" aria-controls="cl-sidebar"
          :aria-expanded="navOpen" aria-label="打开导航" @click="navOpen = !navOpen">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16" /></svg>
        </button>
        <div class="crumb"><span class="crumb-prefix">CourseLoop</span><span class="crumb-sep">/</span><b>{{ currentSection }}</b></div>
        <div class="top-actions">
          <CLThemeSwitch />
          <CLCourseSwitch />
        </div>
      </header>

      <main class="content">
        <!-- 体验模式提示（I-07）：助教 / 教师 / 教务打开学生页面时常驻。说清两件事：
             这是学生的功能；在这里做的题只算自己的，不进任何班级统计（后端的班级统计、
             热力图、排行都只认 role=student）。并给一条回工作台的路。 -->
        <div v-if="isStaff && isStudentPage" class="mode-bar" role="note">
          <span class="mode-tag">学生视角 · 体验模式</span>
          <span class="mode-text">你正在以{{ auth.roleLabel }}身份使用学生功能，这里的作答只记在你自己名下，不计入任何班级统计。</span>
          <router-link class="mode-back" :to="{ name: 'teaching' }">回到助教工作台</router-link>
        </div>
        <!-- keep-alive：切走再回来不该丢东西。没有它时 router-view 会销毁组件实例 ——
             提问到一半切去看错题本，回来对话就空了；正在作答的题、刚生成的复习计划
             同样清零。学习本来就是来回跳着做的，这个丢失很致命。
             嵌套顺序固定：transition > keep-alive > component。
             注意被缓存的组件不再触发 onMounted，需要"回来时刷新"的页面改用
             onActivated —— 否则会换成另一个 bug：刚做错的题，回错题本却看不到。 -->
        <router-view v-slot="{ Component }">
          <transition name="page" mode="out-in">
            <keep-alive>
              <component :is="Component" />
            </keep-alive>
          </transition>
        </router-view>
      </main>
    </div>
  </div>
</template>

<style scoped>
.shell { position: relative; display: flex; height: 100%; background: var(--cl-bg); }

/* ---------- 氛围层 ---------- */
/* 配方与 CLAuthShell 同源（都取自 --cl-aurora-* / --cl-grain / --cl-vignette），
   但强度按下来一档：登录页只有一张卡，光晕可以放肆；应用内页面上有十几张
   玻璃卡片要读，底噪太强会把正文的对比度吃掉。 */
.bg { position: fixed; inset: 0; z-index: 0; pointer-events: none; overflow: hidden; }
.bg::before {
  content: '';
  position: absolute; inset: 0;
  background: var(--cl-scene);
}
.glow { position: absolute; display: block; border-radius: 50%; filter: blur(90px); }
.g1 {
  width: 42vw; height: 42vw; top: -16%; right: -6%;
  background: radial-gradient(circle, var(--cl-aurora-1), transparent 68%);
  opacity: .5;
  animation: drift-a 30s ease-in-out infinite;
}
.g2 {
  width: 48vw; height: 48vw; bottom: -24%; left: -12%;
  background: radial-gradient(circle, var(--cl-aurora-2), transparent 68%);
  opacity: .45;
  animation: drift-b 38s ease-in-out infinite;
}
/* 只动 transform，走合成层，不触发重排重绘 —— 这两团光要一直漂，
   任何一帧的布局开销都会乘以整个会话时长。 */
@keyframes drift-a {
  0%, 100% { transform: translate3d(0, 0, 0) scale(1); }
  50%      { transform: translate3d(-8%, 7%, 0) scale(1.12); }
}
@keyframes drift-b {
  0%, 100% { transform: translate3d(0, 0, 0) scale(1.04); }
  50%      { transform: translate3d(10%, -8%, 0) scale(.93); }
}
/* 混合模式跟着底色换：overlay 只在深底上成立，压在浅底上会把整幅画面洗灰 */
.grain {
  position: absolute; inset: 0;
  opacity: var(--cl-grain-opacity);
  mix-blend-mode: var(--cl-grain-blend);
  background-image: var(--cl-grain);
}
.vignette { position: absolute; inset: 0; background: var(--cl-vignette); }

/* ---------- 侧栏 ---------- */
/* 整块走 --cl-nav-*，不碰 --cl-text / --cl-surface：日间档这里是"深蓝底白字"，
   而右边的内容区是"白底深字"，两边前景正好相反。往侧栏里加东西时记得用 nav 组，
   用错了在夜间档看不出问题（两组值那时是一样的），切到日间就白字压白底。
   侧栏和顶栏是全站仅有的两个该用 backdrop-filter 的常驻表面：它们固定不动、
   内容在下面滚过去，模糊能真正表达"这是一层玻璃压在内容上"。
   卡片不给模糊的原因见 CLCard。 */
.sidebar {
  position: relative; z-index: 2;
  width: 236px; flex-shrink: 0; display: flex; flex-direction: column;
  padding: 22px 14px; gap: 20px;
  background: var(--cl-nav-bg);
  color: var(--cl-nav-text);
  backdrop-filter: var(--cl-blur);
  -webkit-backdrop-filter: var(--cl-blur);
  border-right: 1px solid var(--cl-nav-border);
}
/* 右缘那条竖着的发丝线，把侧栏和内容区切开。夜间档它是一道高光（只用 border
   在深色下几乎看不见）；浅色两档 --cl-nav-edge 是一条淡实线，同一条 CSS 两用。
   上下两端仍然渐隐，否则线头会在圆角和页脚处露出突兀的断口。 */
.sidebar::after {
  content: '';
  position: absolute; top: 0; bottom: 0; right: -1px; width: 1px;
  background: linear-gradient(180deg, transparent, var(--cl-nav-edge) 22%, var(--cl-nav-border) 70%, transparent);
  pointer-events: none;
}

.brand {
  display: flex; align-items: center; gap: 10px; padding: 4px 10px;
  flex-shrink: 0;
  border-radius: var(--cl-radius-sm);
  transition: background .14s ease;
}
.brand:hover { background: var(--cl-nav-hover); }
/* 字标压在 --cl-nav-bg 上，用的是 --cl-nav-brand-grad 而不是页面标题那条
   --cl-text-grad：日间档两者的底色是反的，共用一条必然有一边看不见 */
.brand-name {
  font-size: 19px; font-weight: 800; letter-spacing: .4px;
  background: var(--cl-nav-brand-grad);
  -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent;
}
.brand-sub { font-size: 11.5px; color: var(--cl-nav-text-dim); margin-top: 1px; }

/* 导航是侧栏里唯一会长的那一段，所以滚动条归它，字标和用户卡片钉在两头。
   原先整个侧栏一起溢出：助教和教务能看到 15 个条目（学习 12 + 助教 1 + 教务 2），
   一列排下来九百多像素，1366×768 这类屏幕上用户卡片直接被顶到视口外 ——
   要往下拖整页才看得到头像和退出，而侧栏本该是不动的那一半。

   min-height:0 不能省：flex 子项的 min-height 默认是 auto，不写这一条，
   .nav 不会收缩到内容高度以下，overflow 也就永远不触发，改了等于没改。 */
.nav {
  display: flex; flex-direction: column; gap: 18px;
  flex: 1 1 auto; min-height: 0; overflow-y: auto;
  /* 左右各借 4px：overflow-y:auto 会把横向一并裁掉，不留出这点余量，
     选中条那圈辉光会被切掉半边。用负边距抵回去，条目的视觉位置不变。 */
  margin: 0 -4px; padding: 0 4px;
  /* 全局滚动条那套颜色是按内容区底色调的（--cl-border-strong），
     压在深色侧栏上几乎看不见；这里换成侧栏自己那组。 */
  scrollbar-color: var(--cl-nav-edge) transparent;
}
.nav::-webkit-scrollbar { width: 6px; }
.nav::-webkit-scrollbar-thumb {
  background: var(--cl-nav-edge); border-radius: 999px; border: none;
}
.nav::-webkit-scrollbar-thumb:hover { background: var(--cl-nav-text-dim); }
.nav-group { display: flex; flex-direction: column; gap: 3px; }
.nav-label { font-size: 11px; font-weight: 700; letter-spacing: 1px; color: var(--cl-nav-text-dim); padding: 0 12px 4px; text-transform: uppercase; }
/* 折叠段的段名：外观和别的段名一样，但它是可点的。按钮要显式清掉浏览器默认样式，
   否则在侧栏这块深底上会露出一个浅色方块。 */
.fold-toggle {
  display: flex; align-items: center; gap: 6px; width: 100%;
  border: none; background: transparent; font: inherit; cursor: pointer;
  font-size: 11px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase;
  color: var(--cl-nav-text-dim);
  border-radius: 6px; padding: 3px 12px 5px;
  transition: color .14s ease, background .14s ease;
}
.fold-toggle:hover { color: var(--cl-nav-text-strong); background: var(--cl-nav-hover); }
.fold-toggle:focus-visible { outline: 2px solid var(--cl-nav-bar); outline-offset: 1px; }
/* 收起时把条目数摆出来：一个只写着"进阶与回看"的标题，看不出底下还有没有东西 */
.fold-count {
  font-size: 10.5px; font-weight: 700; letter-spacing: 0;
  padding: 1px 6px; border-radius: 999px;
  background: var(--cl-nav-surface); color: var(--cl-nav-text-dim);
}
/* 段名吃掉剩余宽度，计数和箭头贴右。不靠给箭头 margin-left:auto ——
   那样计数只能排在箭头右边，展开后它又不渲染，右端的东西会横跳一下。 */
.fold-name { margin-right: auto; }
.chev {
  font-size: 15px; line-height: 1;
  transform: rotate(90deg); transition: transform .16s ease;
}
.chev.open { transform: rotate(-90deg); }
.nav-item {
  position: relative; display: flex; align-items: center;
  padding: 10px 14px; border-radius: var(--cl-radius-sm);
  color: var(--cl-nav-text); font-weight: 600; font-size: 14.5px;
  border: 1px solid transparent;
  transition: background .14s ease, color .14s ease, border-color .14s ease;
}
/* 选中态的竖条：只换文字颜色的话，一列六个条目扫过去很难一眼定位当前在哪。
   夜间/日间是发光的青条（两档的侧栏底都是深的，光成立）；
   护眼档 --cl-nav-bar-glow 是 none，退成一条暖色实条 —— 那一档的定义就是不发光。 */
.nav-item .bar {
  position: absolute; left: 4px; top: 50%;
  transform: translateY(-50%) scaleY(0);
  width: 3px; height: 18px; border-radius: 3px;
  background: var(--cl-nav-bar);
  box-shadow: var(--cl-nav-bar-glow);
  transition: transform .16s ease;
}
.nav-item:hover { background: var(--cl-nav-hover); color: var(--cl-nav-text-strong); }
.nav-item.active {
  color: var(--cl-nav-active-text);
  background: var(--cl-nav-active-bg);
  border-color: var(--cl-nav-active-border);
}
.nav-item.active .bar { transform: translateY(-50%) scaleY(1); }

/* ---------- 用户卡片 ---------- */
.user-box {
  margin-top: auto; flex-shrink: 0; display: flex; align-items: center; gap: 10px;
  padding: 10px 12px; border-radius: var(--cl-radius-sm);
  background: var(--cl-nav-surface);
  border: 1px solid var(--cl-nav-border); cursor: pointer;
  transition: background .14s ease, border-color .14s ease, box-shadow .14s ease;
}
.user-box:hover, .user-box:focus-visible {
  background: var(--cl-nav-hover);
  border-color: var(--cl-nav-edge);
  box-shadow: var(--cl-glow); outline: none;
}
.user-link { flex: 1; min-width: 0; display: flex; align-items: center; gap: 10px; }
/* 头像光环：外面一圈半透明靛蓝，让头像从玻璃底上"托"起来。
   用 box-shadow 的扩散环而不是 border —— border 会挤掉 34px 里的内容尺寸。 */
.avatar {
  width: 34px; height: 34px; border-radius: 50%; flex-shrink: 0;
  display: grid; place-items: center; color: var(--cl-on-brand); font-weight: 700;
  background: linear-gradient(135deg, var(--cl-brand), var(--cl-accent));
  box-shadow: 0 0 0 2px var(--cl-nav-edge), var(--cl-shadow-sm);
  transition: box-shadow .14s ease;
}
.user-box:hover .avatar { box-shadow: 0 0 0 2px var(--cl-nav-active-border), var(--cl-glow); }
.avatar img { width: 100%; height: 100%; object-fit: cover; border-radius: inherit; }
.user-meta { flex: 1; min-width: 0; }
.user-name { font-size: 14px; font-weight: 700; color: var(--cl-nav-text-strong); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.user-role { font-size: 12px; color: var(--cl-nav-text-dim); white-space: nowrap; }
.enter { color: var(--cl-nav-text-dim); font-size: 22px; line-height: 1; transition: transform .14s ease, color .14s ease; }
.user-box:hover .enter { color: var(--cl-nav-text-strong); transform: translateX(2px); }
.logout { border: none; background: transparent; cursor: pointer; font: inherit; font-size: 12.5px; color: var(--cl-nav-text-dim); border-radius: 6px; padding: 2px 4px; }
/* 退出是唯一在侧栏里用语义色的地方，也是最容易出事的一处：--cl-err 是按**页面底色**
   调的（浅色两档里它是深红 #dc2626），压在深侧栏上只有 2.8 —— 所以侧栏另备一支
   --cl-nav-danger，三档都是压得住 nav-bg 的浅红。 */
.logout:hover { color: var(--cl-nav-danger); background: var(--cl-nav-hover); }

/* ---------- 主区 ---------- */
.main { position: relative; z-index: 1; flex: 1; display: flex; flex-direction: column; min-width: 0; }
/* 顶栏在日间/护眼档是浅底（与内容区同色系），只有夜间档是深玻璃 ——
   所以它走 --cl-topbar-*，而不是跟侧栏共用 --cl-nav-*。 */
.topbar {
  height: 60px; flex-shrink: 0; display: flex; align-items: center; justify-content: space-between;
  padding: 0 32px; border-bottom: 1px solid var(--cl-topbar-border);
  background: var(--cl-topbar-bg);
  backdrop-filter: var(--cl-blur);
  -webkit-backdrop-filter: var(--cl-blur);
  position: sticky; top: 0; z-index: 10;
}
/* 底缘高光，与侧栏的右缘是同一条线的两段。浅色两档 --cl-topbar-edge 是
   transparent —— 浅底上高光不成立，那里只靠上面那条 border 分界。 */
.topbar::after {
  content: '';
  position: absolute; left: 0; right: 0; bottom: -1px; height: 1px;
  background: linear-gradient(90deg, var(--cl-topbar-edge), transparent 62%);
  pointer-events: none;
}
.crumb { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13.5px; font-weight: 500; color: var(--cl-text-3); letter-spacing: .2px; }
.crumb b { font-weight: 700; color: var(--cl-text-2); }
.crumb-sep { margin: 0 8px; opacity: .5; }
.top-actions { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
.topbar { gap: 12px; }

/* 汉堡按钮与遮罩只在窄屏出现，宽屏下整块不渲染进布局 */
.menu-btn {
  display: none; flex-shrink: 0; align-items: center; justify-content: center;
  width: 38px; height: 38px; padding: 0;
  border: 1px solid var(--cl-glass-border); border-radius: var(--cl-radius-sm);
  background: var(--cl-inset); color: var(--cl-text-2); cursor: pointer;
}
.menu-btn:hover { color: var(--cl-brand-600); border-color: var(--cl-tint-border-strong); background: var(--cl-tint); }
.menu-btn:focus-visible { outline: 2px solid var(--cl-brand); outline-offset: 2px; }
.menu-btn svg { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 2.2; stroke-linecap: round; }
.nav-backdrop { display: none; }

.mode-bar {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  margin: 0 0 16px; padding: 8px 12px;
  border: 1px solid var(--cl-tint-border); border-left: 3px solid var(--cl-brand);
  border-radius: var(--cl-radius-sm); background: var(--cl-tint);
  font-size: 12.5px; color: var(--cl-text-2);
}
.mode-tag { font-weight: 700; color: var(--cl-brand-700); white-space: nowrap; }
.mode-text { flex: 1 1 260px; min-width: 0; }
.mode-back { font-weight: 700; color: var(--cl-brand-600); white-space: nowrap; }
.mode-back:hover { text-decoration: underline; }

/* 网格底纹留在滚动容器里：静止的光晕 + 跟着滚的网格＝纵深。
   background 不加颜色，让下面的氛围层透上来。 */
.content { flex: 1; overflow: auto; padding: 30px 32px; background: var(--cl-page-bg); }

.page-enter-active, .page-leave-active { transition: opacity .2s ease, transform .2s ease; }
.page-enter-from { opacity: 0; transform: translateY(8px); }
.page-leave-to { opacity: 0; transform: translateY(-4px); }

@media (max-width: 900px) { .content { padding: 24px 20px; } .topbar { padding: 0 20px; } }
/* 窄屏：侧栏从「一列」改成「抽屉」。
   原先 ≤720px 只把侧栏压到 156px，480px 宽的手机上它仍吃掉三分之一，
   顶栏剩下不到 300px 塞面包屑 + 两个切换器，按钮文字被挤成竖排（测试报告 I-01）。
   现在侧栏 fixed 浮在内容上方、默认移出视口，正文拿到整屏宽度；
   宽度保持 260px 不压缩，用户卡片等内部排版沿用宽屏那套，不用再单独适配。 */
@media (max-width: 768px) {
  .sidebar {
    position: fixed; top: 0; bottom: 0; left: 0; z-index: 60;
    width: min(260px, 82vw);
    padding-top: calc(22px + env(safe-area-inset-top, 0px));
    padding-bottom: calc(22px + env(safe-area-inset-bottom, 0px));
    transform: translateX(-100%); visibility: hidden;
    transition: transform .22s ease, visibility 0s linear .22s;
    box-shadow: none;
  }
  .sidebar.open {
    transform: none; visibility: visible;
    transition: transform .22s ease;
    box-shadow: 0 0 40px rgba(0, 0, 0, .35);
  }
  .nav-backdrop {
    display: block; position: fixed; inset: 0; z-index: 55;
    background: rgba(8, 12, 24, .45);
    opacity: 0; pointer-events: none; transition: opacity .22s ease;
  }
  .nav-backdrop.show { opacity: 1; pointer-events: auto; }
  .menu-btn { display: inline-flex; }
  .topbar { padding: 0 16px; gap: 10px; }
  .content { padding: 20px 16px; }
}
/* 很窄时连「CourseLoop /」前缀也让出来，只留段名 */
@media (max-width: 480px) {
  .crumb-prefix, .crumb-sep { display: none; }
  .top-actions { gap: 8px; }
}

/* 持续动画一律可关。光晕停在初始位置当静态氛围，不是删掉 —— 页面还是要有底色。 */
@media (prefers-reduced-motion: reduce) {
  .glow { animation: none; }
  .sidebar, .nav-backdrop { transition: none !important; }
  .page-enter-active, .page-leave-active { transition: none; }
}
</style>
