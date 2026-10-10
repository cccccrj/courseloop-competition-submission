#!/usr/bin/env node
/* 深色主题对比度审计 · node scripts/contrast-audit.mjs
 *
 * 为什么需要这个脚本：深色板上几乎每个表面都是半透明的（--cl-glass 是渐变，
 * --cl-row 是 5% 的提亮膜，输入框是 55% 的压暗膜），令牌里写的色值和眼睛看到的
 * 像素色不是一回事。靠肉眼在设计稿上比对会漏，靠浏览器 devtools 又得先有浏览器。
 * 这里按 CSS 的 source-over 规则把图层合成出来再算 WCAG 对比度，改完令牌能立刻回归。
 *
 * 色值从 theme.css 解析，不在这里硬编码 —— 否则改了令牌而忘记改脚本，
 * 审计就会对着过期的值报"通过"。
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(resolve(here, '../src/theme/theme.css'), 'utf8');

/* ── 从 theme.css 取令牌 ── */
const token = (name) => {
  const m = css.match(new RegExp(`--${name}:\\s*([^;]+);`));
  if (!m) throw new Error(`theme.css 里找不到 --${name}`);
  return m[1].trim();
};

/* ── 颜色与合成 ── */
const rgba = (r, g, b, a = 1) => ({ r, g, b, a });
const parse = (s) => {
  s = s.trim();
  let m = s.match(/^#([0-9a-f]{6})$/i);
  if (m) { const h = m[1]; return rgba(...[0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16))); }
  m = s.match(/^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:[\s,/]+([\d.]+))?\s*\)$/i);
  if (m) return rgba(+m[1], +m[2], +m[3], m[4] === undefined ? 1 : +m[4]);
  throw new Error(`无法解析颜色：${s}`);
};
// src 叠在 dst 上，dst 视为不透明
const over = (s, d) => rgba(s.r * s.a + d.r * (1 - s.a), s.g * s.a + d.g * (1 - s.a), s.b * s.a + d.b * (1 - s.a), 1);
const stack = (...layers) => layers.reduce((d, s) => over(s, d));
// element opacity：整个元素（含其文字）一起降透明再合成到背景
const dim = (c, o) => rgba(c.r, c.g, c.b, o);

const lum = (c) => {
  const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
};
const contrast = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
const show = (c) => `rgb(${[c.r, c.g, c.b].map((v) => Math.round(v)).join(', ')})`;

/* 渐变令牌取所有色标，逐个校验 —— 只看中点会漏掉最亮/最暗的一端，
   而白字读不出来恰恰发生在最亮那一端。 */
const gradientStops = (value) => (value.match(/#[0-9a-f]{6}|rgba?\([^)]+\)/gi) || []).map(parse);

/* ── 图层 ── */
const BG = parse(token('cl-bg'));
// --cl-glass 是 168deg 渐变：顶端亮、底端暗，两端都要验
const [GLASS_A, GLASS_B] = gradientStops(token('cl-glass'));
const cardHi = stack(BG, GLASS_A);
const cardLo = stack(BG, GLASS_B);
const [G2A, G2B] = gradientStops(token('cl-glass-2'));
const card2Hi = stack(BG, G2A), card2Lo = stack(BG, G2B);

const ROW = parse(token('cl-row'));
const rowHi = stack(cardHi, ROW);   // 卡片亮端上的行 —— 全站对比度最紧的背景
const rowLo = stack(cardLo, ROW);
const INPUT = stack(cardLo, parse('rgba(6, 9, 20, .55)'));
const CODE = stack(cardLo, parse(token('cl-code-bg')));

const TEXT = parse(token('cl-text'));
const TEXT_2 = parse(token('cl-text-2'));
const TEXT_3 = parse(token('cl-text-3'));
const WHITE = parse('#ffffff');

const AA = 4.5, AA_LARGE = 3.0;
const results = [];
const check = (label, fg, bg, min = AA) => {
  results.push({ label, ratio: contrast(fg, bg), min });
};

/* ── 检查项 ── */
// 正文三级 × 各种表面。行叠在卡片亮端上是最紧的一档，其余都比它宽松。
for (const [bgName, bg] of [['卡面亮端', cardHi], ['卡面暗端', cardLo], ['行(卡亮端)', rowHi], ['行(卡暗端)', rowLo], ['输入凹槽', INPUT], ['代码块', CODE]]) {
  check(`text / ${bgName}`, TEXT, bg);
  check(`text-2 / ${bgName}`, TEXT_2, bg);
  check(`text-3 / ${bgName}`, TEXT_3, bg);
}

// 品牌渐变：七个使用点全部压白字，每个色标都要过
gradientStops(token('cl-brand-grad')).forEach((stop, i) => {
  check(`白字 / brand-grad 色标${i + 1} ${show(stop)}`, WHITE, stop);
});

// 徽标五变体
for (const k of ['ok', 'warn', 'err', 'cite']) {
  check(`badge.${k}`, parse(token(`cl-${k}-fg`)), parse(token(`cl-${k}-bg`)));
}
check('badge.neutral', TEXT_2, stack(cardLo, parse(token('cl-neutral-bg'))));

// 整条降档的三处（Records .82 / Teaching .84 / Review .74）。
// 已在各自 .vue 里把最弱一级提到 text-2，这里验的就是提完之后的值。
for (const [name, o, bg] of [['Records .resolved', .82, rowLo], ['Teaching .reviewed', .84, rowLo], ['Review .task.done', .74, card2Lo]]) {
  check(`${name} 内 text-2 @${o}`, stack(bg, dim(TEXT_2, o)), bg);
}

// 禁用主按钮：压平后的表面，字要读得出、形状要还在
const btnOff = stack(cardLo, parse('rgba(99, 102, 241, .2)'));
check('禁用按钮 字/底', TEXT_3, btnOff);
check('禁用按钮 描边/底', stack(btnOff, parse(token('cl-glass-border'))), btnOff, 1.1);

/* ── 输出 ── */
const pad = (s, n) => { // 中文按两格宽计算，否则列对不齐
  const w = [...String(s)].reduce((a, ch) => a + (ch.charCodeAt(0) > 0x2e80 ? 2 : 1), 0);
  return String(s) + ' '.repeat(Math.max(1, n - w));
};
let failed = 0;
console.log(pad('检查项', 40) + pad('对比度', 10) + pad('阈值', 8) + '结果');
console.log('─'.repeat(66));
for (const r of results) {
  const ok = r.ratio >= r.min;
  if (!ok) failed++;
  console.log(pad(r.label, 40) + pad(r.ratio.toFixed(2), 10) + pad(r.min.toFixed(1), 8) + (ok ? '通过' : '✗ 不足'));
}
console.log('\n合成后的实际表面色：');
for (const [n, c] of [['卡面亮端', cardHi], ['卡面暗端', cardLo], ['行(卡亮端)', rowHi], ['输入凹槽', INPUT], ['代码块', CODE], ['禁用按钮', btnOff]]) {
  console.log('  ' + pad(n, 12) + show(c));
}
console.log(`\n${results.length} 项检查，${failed} 项不足。`);
process.exit(failed ? 1 : 0);
