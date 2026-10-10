<script setup lang="ts">
// 品牌 logomark：三节点连成环，呼应 "Loop"，切合数据结构主题
//
// tone：
//   brand  跟随品牌靛蓝（内容区里的行内用法）
//   nav    压在侧栏上，走 --cl-nav-logo
//   scene  压在认证页场景层上，走 --cl-auth-fg
//
// nav 这一档是三档主题带来的：--cl-brand-600 是按**页面底色**调的（浅色两档里
// 它是深靛蓝 #4338ca），压在日间的深蓝侧栏 / 护眼的深棕侧栏上只有 1.5，几乎消失。
// 侧栏的前景必须走 nav 组，理由同 MainLayout 里那条注释。
//
// scene 这一档原先叫 white，值是写死的 #ffffff —— 那时认证页恒定深色。
// 现在登录页跟随三档主题，日间/护眼的场景是浅底，纯白 logo 会直接消失，
// 所以改成按档取强前景色，名字也从"是什么颜色"换成"压在什么上面"。
withDefaults(defineProps<{ size?: number; tone?: 'brand' | 'nav' | 'scene' }>(), { size: 32, tone: 'brand' })
</script>

<template>
  <span class="cl-logo" :class="tone" :style="{ width: size + 'px', height: size + 'px' }" aria-hidden="true">
    <svg viewBox="0 0 32 32">
      <circle cx="16" cy="7" r="3.2" />
      <circle cx="7" cy="24" r="3.2" />
      <circle cx="25" cy="24" r="3.2" />
      <path d="M16 7 L7 24 M16 7 L25 24 M7 24 L25 24" />
    </svg>
  </span>
</template>

<style scoped>
.cl-logo { display: inline-flex; }
.cl-logo svg { width: 100%; height: 100%; }
.cl-logo.brand { color: var(--cl-brand-600); }
.cl-logo.nav { color: var(--cl-nav-logo); }
.cl-logo.scene { color: var(--cl-auth-fg); }
.cl-logo circle { fill: currentColor; }
/* 连线用青色而不是 currentColor：三个节点和三条边同色时是一团实心三角，
   边压暗一档、换成强调色，"环"的结构才读得出来。 */
.cl-logo path { stroke: var(--cl-accent-soft); stroke-width: 1.6; fill: none; opacity: .8; }
/* 场景档的连线跟着字色走并压一档透明度，保持与 brand 档一样的"节点实、边虚"关系 */
.cl-logo.nav path { stroke: var(--cl-nav-bar); opacity: .85; }
.cl-logo.scene path { stroke: var(--cl-auth-fg); opacity: .6; }
</style>
