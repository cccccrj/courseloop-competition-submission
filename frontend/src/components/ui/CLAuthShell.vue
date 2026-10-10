<script setup lang="ts">
// 登录/注册两页共用的整页外壳（取代早先的 CLAuthAside）。
//
// 为什么从"左插画 + 右表单"改成整页外壳：品牌面里的课程文案去掉之后，左半屏
// 只剩一块渐变色 —— 对半分的版式是为了容纳插画才存在的，插画没了，分栏就成了
// 半屏留白。现在改成一整块画布，把超大字号的 CourseLoop 字标本身当作画面主体，
// 表单卡片偏置到右侧，靠不对称和留白撑住版面，而不是靠一张图。
//
// 三层氛围（沿用旧品牌面里效果最好的部分，全是 CSS/SVG，无图片依赖）：
//   glow    两团缓慢漂移的光晕，给静止页面一点呼吸
//   grain   细颗粒噪声，压掉大面积渐变的塑料味
//   vignette 暗角，把注意力收回画面中部
// 三层一律 position:fixed —— 注册页表单比视口高，用 absolute 会随内容一起拉长，
// 光晕会被抻变形。
//
// 卡片宽度由 wide 控制：登录页两个字段用窄卡，注册页十个字段两两并排要用宽卡。
//
// 表单控件的深色玻璃样式写在下方**非 scoped** 的 style 块里，全部以 .cl-auth 开头。
// 原因：控件（CLField/CLSelect/CLButton…）由调用方放进 slot，slot 内容带的是
// 调用方的 scope id，本组件的 scoped 选择器命中不了；两页各写一份又必然改歪。
import CLThemeSwitch from './CLThemeSwitch.vue'

withDefaults(defineProps<{ wide?: boolean }>(), { wide: false })
</script>

<template>
  <div class="shell cl-auth">
    <!-- 氛围层（fixed，不随注册页的长表单滚动） -->
    <div class="bg" aria-hidden="true">
      <span class="glow g1"></span>
      <span class="glow g2"></span>
      <div class="grain"></div>
      <div class="vignette"></div>
    </div>

    <!-- 皮肤切换器：登录页跟随三档主题，得能在登录之前切。
         钉在视口右上角而不是放进 .brandbar —— brandbar 受 .inner 的 max-width
         约束，宽屏上会被拽离右边缘好几百像素。fixed 之后注册页长表单滚动时
         它也始终可达。 -->
    <div class="themedock"><CLThemeSwitch /></div>

    <div class="inner">
      <header class="brandbar">
        <slot name="logo" />
      </header>

      <div class="stage" :class="{ wide }">
        <!-- 画面主体：超大字标。第二行描边不填充，和第一行的实心渐变形成对比 -->
        <div class="art">
          <p class="mark" aria-hidden="true">
            <span class="m1">Course</span>
            <span class="m2">Loop</span>
          </p>
          <div class="rule"><span class="rdot"></span></div>
        </div>

        <div class="panel">
          <div class="card">
            <slot />
          </div>
        </div>
      </div>

      <footer class="foot">
        <span class="fdot" aria-hidden="true"></span>
        CourseLoop
      </footer>
    </div>
  </div>
</template>

<style scoped>
/* 本组件不出现字面色值，全部走 --cl-* 令牌 —— 这一页现在跟随日间/护眼/夜间三档。
   凡是"强度按档差别很大"的东西都已经上收到 theme.css：场景底 (--cl-scene-auth)、
   空心字标的描边 (--cl-auth-stroke)、细线 (--cl-auth-rule)、卡片的加重阴影
   (--cl-auth-card-shadow)、颗粒的强度与混合模式 (--cl-grain-*)。
   留在本地的只有两类，它们与颜色无关：
     1. 构图 —— 极光的位置/尺寸、字标字号、卡片偏置，属于这一页的版式；
     2. --af-* 尺寸变量 —— 解决的是认证页的表单密度。 */
.shell {
  position: relative;
  height: 100%;
  overflow-y: auto;
  background: var(--cl-bg);
  color: var(--cl-text);
}

/* ---------- 氛围 ---------- */
.bg { position: fixed; inset: 0; pointer-events: none; overflow: hidden; }
.bg::before {
  content: '';
  position: absolute; inset: 0;
  /* 两道径向光晕原先写在这里，现在并进 --cl-scene-auth ——
     它们的强度按档差别很大（夜间是真光晕，浅色两档只是一层极淡的冷色染），
     留在组件里就没法按主题换。位置/尺寸也一并进去了：这一页只有一个场景，
     不存在"同一配方铺到别处"的复用需求。 */
  background: var(--cl-scene-auth);
}
/* 光晕的**颜色**走令牌（与 MainLayout 的氛围层同源），位置和尺寸留在本地：
   那是这一页的构图，换页面就得重排。 */
.glow { position: absolute; display: block; border-radius: 50%; filter: blur(80px); }
.g1 {
  width: 44vw; height: 44vw; top: -12%; left: 48%;
  background: radial-gradient(circle, var(--cl-aurora-1), transparent 68%);
  animation: drift-a 26s ease-in-out infinite;
}
.g2 {
  width: 52vw; height: 52vw; bottom: -22%; left: -14%;
  background: radial-gradient(circle, var(--cl-aurora-2), transparent 68%);
  animation: drift-b 32s ease-in-out infinite;
}
@keyframes drift-a {
  0%, 100% { transform: translate3d(0, 0, 0) scale(1); }
  50%      { transform: translate3d(-9%, 8%, 0) scale(1.14); }
}
@keyframes drift-b {
  0%, 100% { transform: translate3d(0, 0, 0) scale(1.05); }
  50%      { transform: translate3d(11%, -9%, 0) scale(.92); }
}
/* 颗粒：内联 feTurbulence，不引外部图。配方在 --cl-grain */
.grain {
  position: absolute; inset: 0;
  /* 登录页只有一张卡要读，颗粒可以比应用内重一档；混合模式必须跟着底色换，
     overlay 只在深底上成立，压在浅底上会把整幅画面洗灰。 */
  opacity: calc(var(--cl-grain-opacity) * 1.5);
  mix-blend-mode: var(--cl-grain-blend);
  background-image: var(--cl-grain);
}
.vignette { position: absolute; inset: 0; background: var(--cl-vignette); }

/* ---------- 骨架 ---------- */
.inner {
  position: relative;
  min-height: 100%;
  max-width: 1660px; margin: 0 auto;
  padding: 36px clamp(28px, 3.2vw, 60px) 30px;
  display: grid;
  grid-template-rows: auto 1fr auto;
  gap: 24px;
}

.brandbar {
  position: relative; z-index: 60;
  display: flex; align-items: center; gap: 12px; animation: fade-in .35s ease both;
}
/* 切换器脱离 .inner 的 max-width，直接贴视口右上角。z-index 要高于卡片
   （卡片带 backdrop-filter，自成层叠上下文），否则下拉面板会被压在卡下面。 */
.themedock {
  position: fixed; z-index: 60;
  top: 30px; right: clamp(16px, 2.2vw, 34px);
}
/* 字标和 logo 压在场景层上（不是压在主色上），走 --cl-auth-fg：
   夜间是白，浅色两档是深色。用 --cl-on-brand 会在日间档白字压白底。 */
.brandbar :deep(.wordmark) {
  font-size: 17px; font-weight: 800; letter-spacing: .7px; color: var(--cl-auth-fg);
}

/* 1fr 行的最小高度是 auto，注册页的长卡片会把这一行顶高、外壳滚动，
   不会像 flex 居中那样把卡片顶部裁掉。 */
.stage {
  display: grid;
  grid-template-columns: 1fr minmax(372px, 412px);
  gap: clamp(48px, 6vw, 120px);
  align-items: center;
}
.stage.wide { grid-template-columns: 1fr minmax(430px, 536px); gap: 56px; }

/* ---------- 字标 ---------- */
.art { position: relative; }
.mark {
  margin: 0;
  font-size: clamp(58px, 8.4vw, 132px);
  line-height: .86;
  font-weight: 800;
  letter-spacing: -.038em;
  display: flex; flex-direction: column;
}
/* 与页面标题（CLPageHeader）共用 --cl-text-grad，整站的"渐变裁字"只有一个配方 */
.m1 {
  background: var(--cl-text-grad);
  -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent;
  animation: rise .45s cubic-bezier(.22, 1, .36, 1) both;
}
/* 第二行只描边不填充：实心 + 空心的对比是这版版式的主要"设计动作"，
   两行都填实就退回成一块普通的大标题。 */
.m2 {
  color: transparent;
  -webkit-text-stroke: 1.5px var(--cl-auth-stroke);
  animation: rise .45s cubic-bezier(.22, 1, .36, 1) .05s both;
}
.rule {
  position: relative; margin-top: 34px; height: 1px; max-width: 320px;
  background: linear-gradient(90deg, var(--cl-auth-rule), transparent);
  animation: fade-in .4s ease .15s both;
}
/* 光点沿着细线从左漂到右，循环。
   位移用 left 的百分比而不是 transform：细线的宽度是响应式的（窄屏 max-width 只有
   200px），百分比的 left 按父元素宽度解算，换断点不用另写一套关键帧；
   而 transform: translateX(%) 是按自身 5px 解算的，算不出"走满整条线"。
   代价是 left 不走合成层，但这里只有一个 5px 的点，可以忽略。 */
.rdot {
  position: absolute; left: 0; top: 50%;
  width: 5px; height: 5px; margin-left: -2.5px;
  border-radius: 50%; background: var(--cl-accent-soft);
  box-shadow: var(--cl-dot-glow);
  transform: translateY(-50%);
  animation: travel 4.6s cubic-bezier(.45, 0, .55, 1) .6s infinite;
}
/* 拖尾：作为光点的子伪元素，自动跟着走。right:50% 让尾巴从圆心往后拖，
   指向来的方向 —— 有尾巴才像"漂过去"，没有就只是个来回跳的点。 */
.rdot::before {
  content: '';
  position: absolute; right: 50%; top: 50%;
  width: 76px; height: 1px; transform: translateY(-50%);
  background: linear-gradient(270deg, var(--cl-accent-soft), transparent);
  pointer-events: none;
}
/* 两端各留一段淡入淡出，跑回原点的那一跳就看不见了 */
@keyframes travel {
  0%   { left: 0;    opacity: 0; }
  9%   { opacity: 1; }
  84%  { opacity: 1; }
  100% { left: 100%; opacity: 0; }
}

/* ---------- 卡片 ---------- */
.panel { display: flex; justify-content: flex-end; }
/* 玻璃配方走令牌，只有阴影留在本地：这一页只有这一张卡，且是画面的绝对主体，
   要比应用内那些并排的卡片"浮"得更高，用的是加重版而非 --cl-glass-shadow。
   backdrop-filter 在这里也是划算的 —— 全页就一个模糊面，而不是十几个。 */
.card {
  position: relative; width: 100%;
  padding: 38px 36px 32px;
  border-radius: 24px;
  border: 1px solid var(--cl-glass-border);
  background: var(--cl-glass);
  backdrop-filter: var(--cl-blur);
  -webkit-backdrop-filter: var(--cl-blur);
  box-shadow:
    var(--cl-auth-card-shadow);
  animation: card-in .42s cubic-bezier(.22, 1, .36, 1) .03s both;
}
/* 顶缘高光：玻璃质感全靠这一条，去掉就变成普通半透明块 */
.card::before {
  content: '';
  position: absolute; left: 24px; right: 24px; top: 0; height: 1px;
  background: var(--cl-glass-hi);
}

.foot {
  display: flex; align-items: center; gap: 8px;
  font-size: 12px; color: var(--cl-text-3); letter-spacing: .5px;
  animation: fade-in .4s ease .2s both;
}
.fdot {
  width: 6px; height: 6px; border-radius: 50%; background: var(--cl-ok);
  box-shadow: var(--cl-glow-ok);
  animation: blink 2.6s ease-in-out infinite;
}

@keyframes rise {
  from { opacity: 0; transform: translateY(20px); }
  to   { opacity: 1; transform: none; }
}
@keyframes fade-in { from { opacity: 0 } to { opacity: 1 } }
@keyframes card-in {
  from { opacity: 0; transform: translateY(22px) scale(.986); }
  to   { opacity: 1; transform: none; }
}
@keyframes blink { 0%, 100% { opacity: 1 } 50% { opacity: .35 } }

/* ---------- 响应 ---------- */
@media (max-width: 1080px) {
  .inner { padding: 30px 36px 26px; }
  /* 与 .inner 的上内边距联动，保持切换器和左侧 logo 的中线齐平 */
  .themedock { top: 24px; }
  .stage { grid-template-columns: 1fr minmax(340px, 392px); gap: 40px; }
  .stage.wide { grid-template-columns: 1fr minmax(400px, 470px); gap: 36px; }
  .rule { margin-top: 24px; }
}

/* 字标和卡片并排放不下了就上下叠：字标缩到一行大小留在上方，仍然是画面主体 */
@media (max-width: 880px) {
  .inner { padding: 26px 22px 22px; gap: 18px; }
  .themedock { top: 20px; }
  .stage, .stage.wide {
    grid-template-columns: 1fr;
    gap: 26px;
    align-items: start;
  }
  .mark { font-size: clamp(46px, 15vw, 78px); flex-direction: row; gap: .28em; }
  .m2 { -webkit-text-stroke-width: 1.2px; }
  .rule { margin-top: 18px; max-width: 200px; }
  .panel { justify-content: center; }
  .card { padding: 30px 24px 26px; border-radius: 20px; max-width: 460px; }
}

/* 动效一律可关 */
@media (prefers-reduced-motion: reduce) {
  .glow, .m1, .m2, .rule, .rdot, .card, .brandbar, .foot, .fdot { animation: none !important; }
  /* 光点停在起点当静态装饰；拖尾会指向线外，一并去掉 */
  .rdot::before { display: none; }
}
</style>

<!--
  非 scoped：调整 slot 里表单控件的**密度**。
  全部选择器以 .cl-auth 开头，只在本外壳内生效，不会漏到其它页面。

  这一块原先还负责给控件套深色皮 —— 因为当时 CLField/CLSelect/CLSegmented/CLButton
  本身是浅色的，认证页必须自己刷一遍。现在这四个组件已原生是深色玻璃，颜色规则
  全部删掉了（约五十行），只留下认证页独有的尺寸和排版：50px 高的控件、13px 圆角、
  主按钮的字距与加重阴影。这些是"进站第一眼"的版式选择，不该推给全站。
  留着重复的颜色声明是有害的：改 theme.css 时认证页不跟着变，正是漂移的起点。
-->
<style>
/* 尺寸走自定义属性，不靠选择器权重去压 —— 各页要改密度（注册页十个字段要收窄）
   只需在自己的包裹元素上重设变量，继承下来就生效。
   若改成在各页写 :deep(.clf-input){...} 去覆盖，两边算出来的权重是同一档，
   谁生效取决于 chunk 的加载顺序，属于会随打包变动的隐性 bug。 */
.cl-auth {
  --af-h: 50px;          /* 控件高度 */
  --af-fs: 14.5px;       /* 控件字号 */
  --af-pl: 15px;         /* 左内距（登录页要给图标让位） */
  --af-pr: 15px;         /* 右内距（密码框要给眼睛让位） */
  --af-radius: 13px;
  --af-label-fs: 12px;
  --af-label-mb: 7px;
}

/* 下面一律用 `.cl-auth .card` 起头。多带一个类是刻意的：组件自己的 scoped 规则
   （如 .clf-input[data-v-x]）权重是 (0,2,0)，两个类打平会退化成比加载顺序；
   带上 .card 变成 (0,3,0)，稳定压过组件默认皮肤。 */

/* ---------- 输入框与下拉触发器 ---------- */
/* 只改尺寸。颜色、hover、focus、autofill 现在都由 CLField / CLSelect 自带。 */
.cl-auth .card .clf-label,
.cl-auth .card .cls-label {
  font-size: var(--af-label-fs); letter-spacing: .4px; margin-bottom: var(--af-label-mb);
}
.cl-auth .card .clf-input,
.cl-auth .card .cls-trigger {
  height: var(--af-h); padding: 0 var(--af-pr) 0 var(--af-pl); font-size: var(--af-fs);
  border-radius: var(--af-radius);
}

/* ---------- 角色切换 ---------- */
/* 认证页的分段控件比应用内更松（5px 内距、14px 圆角），配合 50px 的输入框高度 */
.cl-auth .card .clseg { padding: 5px; gap: 5px; border-radius: 14px; }
.cl-auth .card .clseg-item { padding: 9px 14px; font-size: 13.5px; border-radius: 10px; }

/* ---------- 主按钮 ---------- */
/* 扫光高光已由 CLButton 自带，这里只保留认证页的形态：撑满卡片宽度的 50px 高、
   1.2px 字距的"大按钮"，以及配得上这个尺寸的加重投影。 */
.cl-auth .card .clb.primary {
  height: var(--af-h); padding: 0; font-size: 15px; letter-spacing: 1.2px;
  border-radius: var(--af-radius);
  box-shadow: var(--cl-glow);
}
.cl-auth .card .clb.primary:hover {
  transform: translateY(-1.5px);
  box-shadow: var(--cl-btn-shadow-hover);
}

/* ---------- 下拉面板 ---------- */
/* 面板本身（玻璃底、模糊、描边、阴影）已与 CLSelect 一致，只跟着卡片调圆角 */
.cl-auth .card .cls-panel { border-radius: 16px; }
.cl-auth .card .cls-search { font-size: 13.5px; padding: 9px 12px; border-radius: 10px; }
.cl-auth .card .cls-item { border-radius: 10px; }
.cl-auth .card .cls-create { border-radius: 0 0 10px 10px; }
</style>
