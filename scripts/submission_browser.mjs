/** Real Chromium navigation, permission and JS-error acceptance; secrets never logged. */
import { createRequire } from 'node:module'
import fs from 'node:fs'
import path from 'node:path'
const require = createRequire(import.meta.url)
const { chromium } = require(process.env.CL_PLAYWRIGHT_MODULE || 'playwright-core')
const root = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..')
const accounts = JSON.parse(fs.readFileSync(path.join(root, '.private/accounts.json'), 'utf8'))
const base = process.env.CL_BROWSER_BASE || 'http://127.0.0.1:18016'
const out = process.env.CL_BROWSER_OUTPUT_DIR
  ? path.resolve(process.env.CL_BROWSER_OUTPUT_DIR)
  : path.join(root, 'artifacts/evidence/ui-' + (process.env.CL_BROWSER_LABEL || 'demo'))
fs.mkdirSync(out, { recursive: true })
const browser = await chromium.launch({ executablePath: process.env.CL_CHROMIUM, headless: true,
  args: ['--no-sandbox', '--disable-dev-shm-usage'] })
const errors = [], results = []
const readinessTimeout = Number(process.env.CL_BROWSER_READY_TIMEOUT_MS || 10000)
const routes = {
  student: ['center','hall','solve','practice','records','review','map','exam','placement','photo-grade','report','community','profile'],
  ta: ['teaching'], teacher: ['teaching'], admin: ['teaching','materials','users'],
}
try {
  for (const [role, paths] of Object.entries(routes)) {
    const context = await browser.newContext({ viewport: { width: 1512, height: 1000 }, reducedMotion: 'reduce' })
    const page = await context.newPage()
    page.on('pageerror', error => { errors.push({ role, error: error.message }); console.log('PAGE_ERROR', role, error.message) })
    page.on('console', msg => { if (msg.type() === 'error') { errors.push({ role, error: msg.text() }); console.log('CONSOLE_ERROR',role,msg.text()) } })
    const account = accounts.find(a => a.role === role)
    await page.goto(base + '/login', { waitUntil: 'networkidle' })
    await page.getByPlaceholder('请输入用户名').fill(account.username)
    await page.getByPlaceholder('请输入密码').fill(account.password)
    await page.getByPlaceholder('请输入密码').press('Enter')
    await page.waitForURL('**/' + (role === 'student' ? 'center' : 'teaching'), { timeout: 60000 })
    results.push({ role, route: 'login-form', passed: true })
    for (const route of paths) {
      const begin = Date.now()
      await page.goto(base + '/' + route, { waitUntil: 'domcontentloaded', timeout: 60000 })
      await page.waitForFunction(() => (document.querySelector('main')?.innerText || '').trim().length > 15, { timeout: 30000 })
      await page.waitForTimeout(1000)
      let ready = true
      try { await page.locator('.sk').first().waitFor({ state: 'hidden', timeout: readinessTimeout }) }
      catch { ready = false; console.log('SKELETON_NOT_READY',role,route) }
      await page.waitForTimeout(500)
      const text = await page.locator('body').innerText()
      const screenshot = role + '-' + route + '.png'
      await page.screenshot({ path: path.join(out, screenshot), fullPage: false, animations: 'disabled', timeout: 60000 })
      results.push({ role, route, final_path: new URL(page.url()).pathname,
        passed: ready && new URL(page.url()).pathname === '/' + route && text.length > 50,
        ms: Date.now()-begin, screenshot })
      console.log('CHECK',role,route,ready ? 'ready' : 'not-ready')
    }
    if (role === 'student') {
      await page.goto(base + '/users', { waitUntil: 'networkidle' })
      results.push({ role, route: 'users-permission', passed: !page.url().endsWith('/users'),
        final_path: new URL(page.url()).pathname })
    }
    await context.close()
  }
} finally {
  await browser.close()
  fs.writeFileSync(path.join(out, 'results.json'), JSON.stringify({ base, readiness_timeout_ms: readinessTimeout, results, page_errors: errors }, null, 2))
}
console.log(JSON.stringify({ total: results.length, passed: results.filter(x => x.passed).length, page_errors: errors.length }))
if (errors.length || results.some(x => !x.passed)) process.exitCode = 1
