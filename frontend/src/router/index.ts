import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { toast } from '@/api/toast'

// 应用内页面（学生 12 + 助教 1 + 教务 2），meta.roles 控制可见性；沿用母项目 beforeEach RBAC 守卫范式。
// 侧边栏由 MainLayout 按这里的顺序、meta.title 与 meta.group 生成，
// 所以这份表的排序就是导航顺序，meta.group 就是侧栏的分段名。
//
// 排序规则：按日常使用频率从高到低，越基础越靠上。四段——
//   ① 入口          中心
//   ② 每天都在做    大厅(问答) → 解题 → 练习 → 错题本 → 复习计划
//   ③ 阶段性回看    知识图谱 → 真题模考 → 入学摸底
//   ④ 辅助与旁支    拍照批改 → 学情周报 → 学习态势
// 入学摸底放在③而不是最上面：它按使用先后确实是第一步，但只做一次，
// 做完就成了天天占据视线的死重量。隐藏页（hideNav）统一挪到列表末尾，
// 免得夹在中间让人误以为侧栏漏了一项。
//
// `fold: true` 就是③④这两段 —— 上面这个"死重量"的判断当初只表达成了排序，
// 十二个条目仍然天天全摊在侧栏上，助教和教务的入口被挤到视口外（见 docs/32）。
// 现在把它表达成折叠：①②常驻，③④收起来，需要时展开。分段沿用这里的原判断，
// 不另立一套 —— 侧栏该显示什么，判据只该有一处。
/** 登录后该落在哪一页。
 *
 * 学生的主场是个性化学习中心；助教、教师、教务的主场是工作台 —— 他们进来是要看
 * 待复核、待干预的，不是来做题的。原先两边都写死 center，助教每次登录都得自己
 * 再点一下侧栏。判据放这里而不是 Login.vue：路由表的 '' 重定向（刷新、直接敲域名）
 * 和登录跳转必须给出同一个答案，分两处写迟早分叉。
 *
 * 角色取自 cl_user，它只是界面快照、可以被改 —— 但改了也没用：真正的越权拦截在
 * 后端（_require_ta）和下面的 beforeEach，这里最多让人落在一个自己进不去的页面上，
 * 然后被 beforeEach 弹回 hall。 */
export function homeRouteName(role?: string): string {
  return role && role !== 'student' ? 'teaching' : 'center'
}

const routes: RouteRecordRaw[] = [
  { path: '/login', name: 'login', component: () => import('@/views/Login.vue'), meta: { public: true } },
  { path: '/register', name: 'register', component: () => import('@/views/Register.vue'), meta: { public: true } },
  {
    path: '/',
    component: () => import('@/layouts/MainLayout.vue'),
    children: [
      { path: '', redirect: () => ({ name: homeRouteName(useAuthStore().user?.role) }) },
      { path: 'center', name: 'center', component: () => import('@/views/Center.vue'), meta: { title: '个性化学习中心' } },
      { path: 'hall', name: 'hall', component: () => import('@/views/Hall.vue'), meta: { title: '学习大厅' } },
      { path: 'solve', name: 'solve', component: () => import('@/views/Solve.vue'), meta: { title: '解题纠错' } },
      { path: 'practice', name: 'practice', component: () => import('@/views/Practice.vue'), meta: { title: '个性化练习' } },
      { path: 'records', name: 'records', component: () => import('@/views/Records.vue'), meta: { title: '错题与学习记录' } },
      { path: 'review', name: 'review', component: () => import('@/views/Review.vue'), meta: { title: '复习计划' } },
      { path: 'map', name: 'map', component: () => import('@/views/KnowledgeMap.vue'), meta: { title: '知识图谱', fold: true } },
      { path: 'exam', name: 'exam', component: () => import('@/views/MockExam.vue'), meta: { title: '真题模考', fold: true } },
      { path: 'placement', name: 'placement', component: () => import('@/views/Placement.vue'), meta: { title: '入学摸底', fold: true } },
      { path: 'photo-grade', name: 'photo-grade', component: () => import('@/views/PhotoGrade.vue'), meta: { title: '拍照批改', fold: true } },
      { path: 'report', name: 'report', component: () => import('@/views/Report.vue'), meta: { title: '学情周报', fold: true } },
      { path: 'community', name: 'community', component: () => import('@/views/Community.vue'), meta: { title: '学习态势', fold: true } },
      { path: 'profile', name: 'profile', component: () => import('@/views/Profile.vue'), meta: { title: '个人信息', hideNav: true } },
      { path: 'teaching', name: 'teaching', component: () => import('@/views/Teaching.vue'), meta: { title: '助教工作台', roles: ['ta', 'teacher', 'admin'], group: '助教' } },
      { path: 'materials', name: 'materials', component: () => import('@/views/Materials.vue'), meta: { title: '资料入库', roles: ['admin'], group: '教务' } },
      { path: 'users', name: 'users', component: () => import('@/views/Users.vue'), meta: { title: '账号管理', roles: ['admin'], group: '教务' } },
    ],
  },
  // 敲错网址：回到这个人自己的主场，而不是一律学习大厅（助教会被扔到学生页）
  { path: '/:pathMatch(.*)*', redirect: () => ({ name: homeRouteName(useAuthStore().user?.role) }) },
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.meta.public) return true
  if (!auth.isLoggedIn) return { name: 'login' }
  const roles = to.meta.roles as string[] | undefined
  if (roles && !roles.includes(auth.user!.role)) {
    // 说清楚为什么被弹回来 —— 此前页面悄悄换掉，用户只会以为是点错了
    toast(`「${String(to.meta.title || '这个页面')}」仅${roles.includes('ta') ? '助教与老师' : '教务'}可用，已回到你的首页`, 'info')
    return { name: homeRouteName(auth.user!.role) }
  }
  return true
})

export default router
