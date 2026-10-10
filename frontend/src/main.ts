import { createApp } from 'vue'
import { createPinia } from 'pinia'
import router from './router'
import App from './App.vue'
import './theme/theme.css'

// 三档主题（日间/护眼/夜间）。data-theme 由 index.html 里的防闪脚本在首帧之前
// 打好，stores/theme.ts 启动时读回结论，这里不需要再初始化一次。
//
// 老键 cl_theme 是"浅色/深色"两档时代的产物，取值语义和现在对不上，继续清掉。
// 新偏好存在 cl_theme_v2。
localStorage.removeItem('cl_theme')

createApp(App).use(createPinia()).use(router).mount('#app')
