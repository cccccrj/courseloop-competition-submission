// usage: node render.cjs in.svg out.png [scale]
const { chromium } = require(process.env.CL_PLAYWRIGHT_MODULE || 'playwright-core');
const fs = require('fs');
(async () => {
  const [inp, out, scale] = process.argv.slice(2);
  const svg = fs.readFileSync(inp, 'utf8');
  const browser = await chromium.launch({
    executablePath: process.env.CL_CHROMIUM || undefined,
    args: ['--no-sandbox'],
  });
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: Number(scale || 2) });
  const font = '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;600;700&display=block">';
  await page.setContent(`<html><head>${font}</head><body style="margin:0;background:#fff">${svg}</body></html>`, { waitUntil: 'networkidle' });
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(500);
  await page.locator('svg').first().screenshot({ path: out });
  await browser.close();
  console.log('ok', out);
})().catch(e => { console.error(e); process.exit(1); });
