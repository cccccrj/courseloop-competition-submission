import { defineStore } from 'pinia'
import type { CLUser } from '@/api/types'
import { TOKEN_KEY } from '@/api/client'

// 身份 = 令牌（授权凭据，后端 HMAC 签名）+ 用户资料快照（只给界面显示用）。
// 两者分开存是有意的：改 cl_user 里的 role 不会让你变成助教，后端只认 cl_token。
const KEY = 'cl_user'

function load(): CLUser | null {
  try {
    const raw = localStorage.getItem(KEY)
    return raw ? (JSON.parse(raw) as CLUser) : null
  } catch {
    return null
  }
}

function loadToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) || ''
  } catch {
    return ''
  }
}

export const useAuthStore = defineStore('auth', {
  state: () => ({ user: load() as CLUser | null, token: loadToken() }),
  getters: {
    // 有令牌才算登录：只剩 cl_user 而没有令牌时，所有带身份的接口都会 401，
    // 与其让用户进去处处碰壁，不如直接判定为未登录、请他重新登录。
    isLoggedIn: (s) => !!s.user?.username && !!s.token,
    isTa: (s) => s.user?.role === 'ta' || s.user?.role === 'teacher',
    isAdmin: (s) => s.user?.role === 'admin',
    // 角色名单独成一个 getter：原先界面上写的是 `isTa ? '助教' : '学生'`，
    // 后端加了 admin 之后，教务管理员登录会被标成「学生」—— 一个非此即彼的
    // 三元式在角色变成四种的那天就必然出错，且不会有任何报错提示。
    roleLabel: (s) => ({ student: '学生', ta: '助教', teacher: '教师', admin: '管理员' }
      [s.user?.role || 'student'] || '学生'),
    displayName: (s) => s.user?.name || s.user?.username || '访客',
  },
  actions: {
    setUser(u: CLUser) {
      this.user = u
      localStorage.setItem(KEY, JSON.stringify(u))
    },
    setSession(u: CLUser, token: string) {
      this.setUser(u)
      this.token = token
      localStorage.setItem(TOKEN_KEY, token)
    },
    logout() {
      this.user = null
      this.token = ''
      localStorage.removeItem(KEY)
      localStorage.removeItem(TOKEN_KEY)
    },
  },
})
