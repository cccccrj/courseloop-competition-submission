import { execSync } from 'node:child_process'
import { fileURLToPath, URL } from 'node:url'
import { defineConfig, type Plugin } from 'vite'
import vue from '@vitejs/plugin-vue'

// 构建版本戳（docs/34 §2.1）：只写进 dist/build.json，不再打进页面 ——
// 交付产品不对用户显示提交号；nginx 对外也不提供这个文件（deploy/nginx.conf）。
// scripts/preflight_demo.py 在服务器本机读它，和后端 /admin/runtime、仓库 HEAD 三方比对。
// 部署机上可能没有 .git，所以先认环境变量 CL_BUILD_COMMIT。
function gitCommit(): string {
  if (process.env.CL_BUILD_COMMIT) return process.env.CL_BUILD_COMMIT.slice(0, 12)
  try {
    return execSync('git rev-parse --short=7 HEAD', { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim()
  } catch {
    return 'unknown'
  }
}
function gitDirty(): boolean {
  if (process.env.CL_BUILD_COMMIT) return false
  try {
    return execSync('git status --porcelain --untracked-files=no', { stdio: ['ignore', 'pipe', 'ignore'] })
      .toString().trim().length > 0
  } catch {
    return false
  }
}
const BUILD = { commit: gitCommit(), dirty: gitDirty(), built_at: new Date().toISOString().slice(0, 19) + 'Z' }

function buildStamp(): Plugin {
  return {
    name: 'courseloop-build-stamp',
    apply: 'build',
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: 'build.json', source: JSON.stringify(BUILD, null, 2) + '\n' })
    },
  }
}

// 前端经隧道/AutoDL 自定义服务访问时，浏览器侧的 127.0.0.1 指向用户本机而非服务器，
// 因此前端统一走同源 /api，由本 dev server 代理到后端 :8000（生产由 Nginx 同样代理）。
export default defineConfig({
  plugins: [vue(), buildStamp()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    rollupOptions: {
      output: {
        // 不配置时 Rollup 会把 katex/marked 这类"只有 MarkdownView 用得到"的重库，
        // 跟 axios/toast 这类"每个页面都要用"的公共模块合并进同一个 chunk ——
        // 实测导致登录页（根本不渲染 Markdown）也要下载 100KB+ gzip 的 katex+marked。
        // 显式拆分，保证登录/注册这类轻页面的首屏 chunk 里不再混入这些重库。
        manualChunks(id) {
          if (!id.includes('node_modules')) return
          if (id.includes('katex')) return 'vendor-katex'
          if (id.includes('marked')) return 'vendor-marked'
          if (id.includes('highlight.js')) return 'vendor-hljs'
        },
      },
    },
  },
  server: {
    host: '0.0.0.0',
    // 固定开发端口，便于本地代理和容器映射。
    port: 6006,
    strictPort: true,
    allowedHosts: (process.env.CL_ALLOWED_HOSTS || 'localhost,127.0.0.1').split(','),
    proxy: {
      '/api': {
        target: process.env.CL_BACKEND_URL || 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
    },
  },
})
