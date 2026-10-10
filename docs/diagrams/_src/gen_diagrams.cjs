// 生成 CourseLoop 三张架构图（SVG，1920x1080，内联属性，便于 PPT 导入编辑）
// usage: node gen.cjs <outDir>
const fs = require('fs');
const path = require('path');
const OUT = process.argv[2];

const FONT = "PingFang SC, Microsoft YaHei, Noto Sans SC, WenQuanYi Zen Hei, sans-serif";
const INK = '#0F172A', SUB = '#475569', MUTED = '#94A3B8';
const C = {
  indigo: { main: '#3B4CCA', tint: '#EEF0FC', soft: '#F7F8FE', line: '#C3C9F2' },
  cyan:   { main: '#0E8A9A', tint: '#E3F5F7', soft: '#F3FBFC', line: '#A6DAE1' },
  amber:  { main: '#C2600A', tint: '#FDF0E1', soft: '#FFF9F2', line: '#EFC28C' },
  green:  { main: '#047857', tint: '#E3F4EC', soft: '#F3FBF7', line: '#A5D9C1' },
  blue:   { main: '#2563EB', tint: '#E7EFFE', soft: '#F5F8FF', line: '#B6CBF7' },
  slate:  { main: '#475569', tint: '#EDF1F5', soft: '#F8FAFC', line: '#CBD5E1' },
  ink:    { main: '#1E293B', tint: '#E2E8F0', soft: '#F8FAFC', line: '#CBD5E1' },
};

const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const f = n => Math.round(n * 10) / 10;

function text(x, y, s, { size = 14, color = INK, weight = 400, anchor = 'start', opacity } = {}) {
  return `<text x="${f(x)}" y="${f(y)}" font-family="${FONT}" font-size="${size}" font-weight="${weight}" fill="${color}" text-anchor="${anchor}"${opacity ? ` fill-opacity="${opacity}"` : ''}>${esc(s)}</text>`;
}
function rect(x, y, w, h, { fill = '#fff', stroke = 'none', sw = 1, rx = 10, dash } = {}) {
  return `<rect x="${f(x)}" y="${f(y)}" width="${f(w)}" height="${f(h)}" rx="${rx}" fill="${fill}" stroke="${stroke}" stroke-width="${sw}"${dash ? ` stroke-dasharray="${dash}"` : ''}/>`;
}

// 卡片：标题 + 若干副行，整体垂直居中
function card({ x, y, w, h, c = 'slate', title, sub = [], strong = false, dashed = false, tSize = 17, sSize = 13, big, align = 'middle', badge }) {
  const k = C[c];
  let o = rect(x, y, w, h, {
    fill: strong ? k.tint : '#FFFFFF', stroke: strong ? k.main : k.line,
    sw: strong ? 2 : 1.3, rx: 12, dash: dashed ? '7 5' : null,
  });
  const lines = [];
  if (big) lines.push({ s: big, size: 30, color: k.main, weight: 700, gap: 10 });
  if (title) lines.push({ s: title, size: tSize, color: strong ? k.main : INK, weight: 700, gap: 9 });
  sub.forEach(s => lines.push({ s, size: sSize, color: SUB, weight: 400, gap: 6 }));
  const H = lines.reduce((a, l, i) => a + l.size + (i ? lines[i - 1].gap : 0), 0);
  let cy = y + (h - H) / 2;
  const tx = align === 'middle' ? x + w / 2 : x + 18;
  lines.forEach((l, i) => {
    cy += (i ? lines[i - 1].gap : 0) + l.size;
    o += text(tx, cy - l.size * 0.14, l.s, { size: l.size, color: l.color, weight: l.weight, anchor: align });
  });
  if (badge) {
    o += `<circle cx="${f(x + 4)}" cy="${f(y + 4)}" r="15" fill="${k.main}" stroke="#fff" stroke-width="3"/>`;
    o += text(x + 4, y + 9, badge, { size: 14, color: '#fff', weight: 700, anchor: 'middle' });
  }
  return o;
}

function chip(x, y, w, h, s, c, { dashed = false, size = 14 } = {}) {
  const k = C[c];
  return rect(x, y, w, h, { fill: k.soft, stroke: k.line, sw: 1.2, rx: 8, dash: dashed ? '6 4' : null })
    + text(x + w / 2, y + h / 2 + size * 0.36, s, { size, anchor: 'middle' });
}

const MARK_COLORS = ['indigo', 'cyan', 'amber', 'green', 'blue', 'slate'];
const defs = `<defs>${MARK_COLORS.map(n =>
  `<marker id="ah-${n}" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="${C[n].main}"/></marker>`).join('')}</defs>`;

function arrow(d, c = 'indigo', { dashed = false, sw = 2, start = false } = {}) {
  return `<path d="${d}" fill="none" stroke="${C[c].main}" stroke-width="${sw}"${dashed ? ' stroke-dasharray="7 5"' : ''} marker-end="url(#ah-${c})"${start ? ` marker-start="url(#ah-${c})"` : ''} stroke-linejoin="round"/>`;
}
// 带白底的连线标签
function tag(x, y, s, c = 'slate', size = 12.5) {
  const w = [...s].reduce((a, ch) => a + (ch.charCodeAt(0) > 255 ? size : size * 0.58), 0) + 14;
  return rect(x - w / 2, y - size * 0.95, w, size * 1.6, { fill: '#fff', rx: 6 })
    + text(x, y + size * 0.36, s, { size, color: C[c].main, weight: 600, anchor: 'middle' });
}

function frame(title, subtitle, body, legend) {
  let o = `<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080" viewBox="0 0 1920 1080">${defs}`;
  o += rect(0, 0, 1920, 1080, { fill: '#FFFFFF', rx: 0 });
  o += rect(60, 50, 7, 56, { fill: C.indigo.main, rx: 3.5 });
  o += text(84, 84, title, { size: 34, weight: 700 });
  o += text(84, 112, subtitle, { size: 17, color: SUB });
  o += body;
  o += text(60, 1052, 'CourseLoop · 专业课学习助手 · 2026-10', { size: 14, color: MUTED });
  // 图例：从右往左排
  let x = 1860;
  legend.slice().reverse().forEach(it => {
    const tw = [...it.label].reduce((a, ch) => a + (ch.charCodeAt(0) > 255 ? 14 : 8), 0);
    x -= tw;
    o += text(x, 1052, it.label, { size: 14, color: SUB });
    x -= 46;
    if (it.kind === 'box') o += rect(x, 1038, 36, 20, { fill: it.strong ? C[it.c].tint : '#fff', stroke: it.strong ? C[it.c].main : C[it.c].line, sw: it.strong ? 2 : 1.3, rx: 5, dash: it.dashed ? '5 4' : null });
    else o += arrow(`M${x} 1047 H${x + 34}`, it.c, { dashed: it.dashed });
    x -= 30;
  });
  return o + '</svg>\n';
}

// 等宽排一行卡片
function row(x0, y, totalW, h, gap, items, extra = {}) {
  const w = (totalW - gap * (items.length - 1)) / items.length;
  return items.map((it, i) => card({ x: x0 + i * (w + gap), y, w, h, ...extra, ...it })).join('');
}

/* ───────────────────────── 图 1：平台分层总览 ───────────────────────── */
function d1() {
  const X = 60, W = 1530, LW = 150, CX = X + LW + 16, CW = X + W - 16 - CX;
  let o = '';
  const band = (y, h, c, label, en) => {
    const k = C[c];
    let s = rect(X, y, W, h, { fill: k.soft, stroke: k.line, sw: 1.2, rx: 14 });
    s += rect(X, y, LW, h, { fill: k.main, rx: 14 }) + rect(X + LW - 14, y, 14, h, { fill: k.main, rx: 0 });
    s += text(X + LW / 2, y + h / 2 + 2, label, { size: 20, weight: 700, color: '#fff', anchor: 'middle' });
    s += text(X + LW / 2, y + h / 2 + 22, en, { size: 11, color: '#fff', anchor: 'middle', opacity: 0.75 });
    return s;
  };

  // L1 用户与角色
  o += band(140, 76, 'slate', '用户与角色', 'USERS');
  o += row(CX, 150, CW, 56, 16, [
    { title: '学生', sub: ['学 · 练 · 测 · 记 · 复 · 看 全流程自主学习'] },
    { title: '助教 / 教师', sub: ['班级学情 · 人工复核 · 共性错因布置作业'] },
    { title: '教务管理员', sub: ['资料自助入库 · 账号批量管理 · 课程配置'] },
  ], { c: 'slate', tSize: 16 });

  // L2 体验层
  o += band(228, 112, 'indigo', '体验层', 'PRESENTATION');
  o += row(CX, 238, CW, 62, 16, [
    { title: '学生端 · 12 个功能页', sub: ['学习中心 · 大厅问答 · 解题纠错 · 练习 · 模考 · 知识图谱'] },
    { title: '助教工作台 · 5 个页签', sub: ['今日 · 班级总览 · 复核队列 · 掌握度热力图 · 作业'] },
    { title: '教务管理端', sub: ['资料入库与 OCR · 账号 CSV 批量导入 · 运行版本'] },
  ], { c: 'indigo', tSize: 16 });
  o += text(CX + CW / 2, 324, 'Vue 3.5  ·  TypeScript  ·  Vite  ·  Pinia  ·  KaTeX 公式  ·  代码高亮  ·  三套主题  ·  会话三层持久化  ·  16 断点响应式  ·  无障碍', { size: 14, color: C.indigo.main, anchor: 'middle', weight: 600 });

  // L3 接入层
  o += band(352, 86, 'indigo', '接入层', 'GATEWAY · API');
  {
    const gap = 40, w = (CW - gap * 3) / 4, y = 362, h = 66;
    const items = [
      { title: 'nginx', sub: ['静态资源 · SSE 反向代理'] },
      { title: 'FastAPI 服务', sub: ['86 个 REST API · 6 条 SSE 流式链路'] },
      { title: '鉴权与权限', sub: ['HMAC-SHA256 签名令牌 · 四角色 RBAC'] },
      { title: '统一异常与清洗', sub: ['中文话术 + 错误编号 · 模型输出清洗'] },
    ];
    items.forEach((it, i) => { o += card({ x: CX + i * (w + gap), y, w, h, c: 'indigo', tSize: 16, ...it }); });
    for (let i = 0; i < 3; i++) {
      const x1 = CX + i * (w + gap) + w + 4, x2 = x1 + gap - 8;
      o += arrow(`M${f(x1)} ${y + h / 2} H${f(x2)}`, 'indigo');
    }
  }

  // L4 智能体层
  o += band(450, 112, 'cyan', '智能体层', 'AGENTS');
  {
    const y = 460, h = 92, tgW = 300, gap = 40;
    o += card({ x: CX, y, w: tgW, h, c: 'cyan', strong: true, title: 'TutorGraph 导学编排', sub: ['自研状态机 · 五级提示 · 七步闭环', '会话快照 · 重启 / 换设备可续接'], tSize: 18 });
    o += arrow(`M${CX + tgW + 4} ${y + h / 2} H${CX + tgW + gap - 4}`, 'cyan');
    o += tag(CX + tgW + gap / 2, y + h / 2 - 18, '调度', 'cyan', 12);
    const x0 = CX + tgW + gap;
    o += row(x0, y, CX + CW - x0, h, 12, [
      { title: '步骤化解题', sub: ['结构化逐步解析', '每步附验证标记'] },
      { title: '错因诊断', sub: ['首错定位', '10 类错因体系'] },
      { title: '变式出题', sub: ['检索生成', '先验证后展示'] },
      { title: '拍照批改', sub: ['视觉模型转写', '接入同一诊断链'] },
      { title: '规划与周报', sub: ['确定性聚合数字', '大模型只做叙述'] },
    ], { c: 'cyan', tSize: 16 });
  }

  // L5 核心能力层
  o += band(574, 206, 'ink', '核心能力层', 'CORE ENGINES');
  {
    const y = 584, h = 186, gap = 16, w = (CW - gap * 2) / 3;
    const panels = [
      { c: 'amber', t: '可信验证引擎', en: 'VERIFIER-FIRST', chips: [['代码沙箱'], ['数值规范判分（7 类）'], ['得分点规则'], ['AST 复杂度分析'], ['引用一致性'], ['统一判题口径']], note: '三态输出：通过 / 不通过 / 无法判定 · 沙箱结论优先' },
      { c: 'blue', t: '知识检索引擎', en: 'COURSE-ISOLATED RAG', chips: [['FTS5 Trigram + BM25'], ['课程隔离 · 按意图开放'], ['4 种分块 · 真实页码'], ['内容哈希去重'], ['bge-m3 语义 + Reranker'], ['扫描件 VLM OCR']], note: '2646 个检索块 · 引用命中率 92.9% · 无卡可运行' },
      { c: 'green', t: '学习科学引擎', en: 'LEARNING SCIENCE', chips: [['掌握度 + 遗忘衰减'], ['间隔复习阶梯'], ['408 分值时间预算'], ['自适应入学摸底'], ['408 真实卷面模考'], ['10 类错因体系']], note: '六状态掌握度 · 可解释 · 为每位学生排出「今日任务」' },
    ];
    panels.forEach((p, i) => {
      const x = CX + i * (w + gap), k = C[p.c];
      o += rect(x, y, w, h, { fill: '#fff', stroke: k.line, sw: 1.3, rx: 12 });
      o += rect(x, y, w, 40, { fill: k.tint, rx: 12 }) + rect(x, y + 26, w, 14, { fill: k.tint, rx: 0 });
      o += text(x + 16, y + 27, p.t, { size: 17, weight: 700, color: k.main });
      o += text(x + w - 16, y + 26, p.en, { size: 11, weight: 600, color: k.main, anchor: 'end', opacity: 0.8 });
      const cw = (w - 32 - 10) / 2;
      p.chips.forEach(([s, dashed], j) => {
        const cx = x + 16 + (j % 2) * (cw + 10), cy = y + 50 + Math.floor(j / 2) * 37;
        o += chip(cx, cy, cw, 30, dashed ? s + '（可切换）' : s, p.c, { dashed, size: 13.5 });
      });
      o += text(x + w / 2, y + 175, p.note, { size: 12.5, color: SUB, anchor: 'middle' });
    });
  }

  // L6 模型层
  o += band(792, 100, 'cyan', '模型层', 'MODELS');
  o += row(CX, 802, CW, 80, 16, [
    { title: 'OpenAI 兼容模型网关', sub: ['统一接入 · 流式输出 · 用量计量'] },
    { title: '四角色模型编排', sub: ['主力 · 专家 · 推理 · 视觉'] },
    { title: '任务级路由', sub: ['诊断 / 解题 / 问答 / 出题 独立配置'] },
    { title: '自研 408 垂类模型', sub: ['Qwen3-14B · SFT + GRPO · 多 LoRA'] },
  ], { c: 'cyan', tSize: 16 });

  // L7 数据层
  o += band(904, 90, 'slate', '数据层', 'DATA');
  o += row(CX, 914, CW, 70, 16, [
    { title: 'SQLite（WAL）', sub: ['24 张业务表 · 每 3 小时在线热备'] },
    { title: 'FTS5 检索索引', sub: ['2646 块 · 100% 带真实页码'] },
    { title: '408 题库', sub: ['4719 题 · 40 套零重复模考卷'] },
    { title: 'LLM 审计日志', sub: ['全量调用 · 验证结果 · token 用量'] },
  ], { c: 'slate', tSize: 16 });

  // 右侧横切能力
  {
    const x = 1620, w = 240, y0 = 140, y1 = 994;
    o += rect(x, y0, w, y1 - y0, { fill: '#F8FAFC', stroke: C.slate.line, sw: 1.2, rx: 14 });
    o += rect(x, y0, w, 56, { fill: C.ink.main, rx: 14 }) + rect(x, y0 + 40, w, 16, { fill: C.ink.main, rx: 0 });
    o += text(x + w / 2, y0 + 28, '横切能力', { size: 20, weight: 700, color: '#fff', anchor: 'middle' });
    o += text(x + w / 2, y0 + 46, 'CROSS-CUTTING', { size: 11, color: '#fff', anchor: 'middle', opacity: 0.75 });
    const blocks = [
      { c: 'indigo', t: '安全', items: ['HMAC-SHA256 签名令牌', '四角色 RBAC 守卫', '模型输出统一清洗', '会话防枚举 · 关闭接口文档'] },
      { c: 'cyan', t: '可观测', items: ['错误编号贯穿话术与日志', 'LLM 调用全量审计', 'token 用量与成本报表'] },
      { c: 'green', t: '质量', items: ['557 个自动化测试用例', 'verify_all 一键体检', '端到端演示冒烟 14/14', '界面文案防泄露测试'] },
      { c: 'slate', t: '运维', items: ['版本四方核对可证明', 'systemd 安全加固', '每 3 小时在线热备份', '带校验和的发布包'] },
    ];
    const top = y0 + 68, gap = 12, bh = (y1 - 12 - top - gap * 3) / 4;
    blocks.forEach((b, i) => {
      const by = top + i * (bh + gap), k = C[b.c];
      o += rect(x + 12, by, w - 24, bh, { fill: '#fff', stroke: k.line, sw: 1.2, rx: 10 });
      o += rect(x + 12, by + 14, 4, 22, { fill: k.main, rx: 2 });
      o += text(x + 26, by + 32, b.t, { size: 18, weight: 700, color: k.main });
      b.items.forEach((s, j) => { o += text(x + 26, by + 62 + j * 27, '· ' + s, { size: 13.5, color: INK }); });
    });
  }
  return frame('CourseLoop 平台分层技术架构', '七层解耦 + 四类横切能力：大模型负责表达，验证器负责正确，学习引擎负责个性化', o, [
    { kind: 'box', c: 'slate', label: '平台组件' },
    { kind: 'box', c: 'amber', strong: true, label: '可信验证' },
    { kind: 'box', c: 'cyan', strong: true, label: 'AI 智能体 / 模型' },
    { kind: 'box', c: 'green', strong: true, label: '学习科学' },
  ]);
}

/* ───────────────────────── 图 2：智能学习闭环 ───────────────────────── */
function d2() {
  let o = '';
  const cx = 705, cy = 592, rx = 385, ry = 288;
  const step = 2 * Math.PI / 7, t0 = (154.3 / 180) * Math.PI;
  const nodes = [
    { c: 'slate', title: '学生作答 / 提问', sub: ['文字 · 代码 · 拍照草稿'] },
    { c: 'cyan', title: 'TutorGraph 编排', sub: ['状态机 · 五级渐进提示'] },
    { c: 'blue', title: '检索教材证据', sub: ['课程隔离 · 附页码引用'] },
    { c: 'cyan', title: '大模型生成', sub: ['讲解 / 解析 / 诊断 · 流式'] },
    { c: 'amber', title: '确定性验证 & 统一判题', sub: ['沙箱 · 数值 · 规则 · 复杂度', '沙箱结论 > 模型判断', '判不了 = 无法判定，不硬判'], strong: true, w: 300, h: 128 },
    { c: 'green', title: '掌握度更新', sub: ['难度加权 · 提示惩罚 · 遗忘衰减'] },
    { c: 'green', title: '复习排期 / 今日任务', sub: ['间隔复习 · 408 分值时间预算'] },
  ];
  const P = t => [cx + rx * Math.cos(t), cy + ry * Math.sin(t)];
  // 环
  o += `<ellipse cx="${cx}" cy="${cy}" rx="${rx}" ry="${ry}" fill="none" stroke="${C.indigo.line}" stroke-width="3"/>`;
  o += `<ellipse cx="${cx}" cy="${cy}" rx="${rx - 70}" ry="${ry - 62}" fill="${C.indigo.soft}" stroke="none"/>`;
  // 环上箭头
  for (let k = 0; k < 7; k++) {
    const t = t0 + (k + 0.5) * step, [x, y] = P(t);
    const ang = Math.atan2(ry * Math.cos(t), -rx * Math.sin(t)) * 180 / Math.PI;
    o += `<path d="M11,0 L-8,-8 L-8,8 z" fill="${C.indigo.main}" transform="translate(${f(x)},${f(y)}) rotate(${f(ang)})"/>`;
  }
  const boxes = nodes.map((n, k) => {
    const [x, y] = P(t0 + k * step), w = n.w || 248, h = n.h || 86;
    return { ...n, x: x - w / 2, y: y - h / 2, w, h, cx: x, cy: y };
  });
  boxes.forEach((b, k) => { o += card({ ...b, badge: String(k + 1), tSize: b.strong ? 18 : 17, sSize: 13 }); });

  // 中心口号
  o += text(cx, cy - 70, 'CourseLoop 学习闭环', { size: 15, color: SUB, anchor: 'middle', weight: 600 });
  [['大模型', '负责表达', 'cyan'], ['验证器', '负责正确', 'amber'], ['学习引擎', '负责个性化', 'green']].forEach(([a, b, c], i) => {
    const y = cy - 22 + i * 46;
    o += `<text x="${cx}" y="${y}" font-family="${FONT}" font-size="27" font-weight="700" text-anchor="middle"><tspan fill="${C[c].main}">${a}</tspan><tspan fill="${INK}">${b}</tspan></text>`;
  });
  o += text(cx, cy + 128, '每一次作答都会更新学情，并决定下一步学什么', { size: 13.5, color: SUB, anchor: 'middle' });

  // 右侧：数据飞轮
  const PX = 1290, PW = 570, IX = 1318, IW = 432;
  const panel = (y, h, c, t, en) => {
    const k = C[c];
    return rect(PX, y, PW, h, { fill: k.soft, stroke: k.line, sw: 1.3, rx: 14 })
      + text(PX + 24, y + 34, t, { size: 19, weight: 700, color: k.main })
      + text(PX + PW - 48, y + 33, en,{ size: 11, weight: 600, color: k.main, anchor: 'end', opacity: 0.8 });
  };
  o += panel(150, 386, 'cyan', '数据飞轮 · 持续进化', 'DATA FLYWHEEL');
  const fy = [200, 278, 356, 434], fh = 56;
  const fly = [
    { title: 'LLM 审计日志', sub: ['每次调用 · 输入输出 · 验证结果 · 用量'] },
    { title: '验证器筛选', sub: ['只保留通过确定性验证的样本'] },
    { title: '训练数据', sub: ['SFT 样本 · 助教改判 → 偏好数据（DPO）'] },
    { title: '自研 408 垂类模型', sub: ['Qwen3-14B · SFT + GRPO · 多 LoRA'] },
  ];
  fly.forEach((it, i) => {
    o += card({ x: IX, y: fy[i], w: IW, h: fh, c: 'cyan', tSize: 16, sSize: 12.5, ...it });
    if (i < 3) o += arrow(`M${IX + IW / 2} ${fy[i] + fh + 3} V${fy[i + 1] - 4}`, 'cyan');
  });
  // 生成 → 审计日志
  const gen = boxes[3];
  o += arrow(`M${f(gen.x + gen.w + 4)} ${f(gen.cy - 14)} H1262 V${fy[0] + fh / 2} H${IX - 4}`, 'cyan');
  o += tag(1172, gen.cy - 14, '全量记录', 'cyan');
  // 自研模型 → 生成（任务级路由接入）
  o += arrow(`M${IX - 4} ${fy[3] + fh / 2} H${f(gen.cx + 70)} V${f(gen.y + gen.h + 6)}`, 'cyan');
  o += tag(1150, fy[3] + fh / 2, '任务级路由接入', 'cyan');

  // 右侧：人在回路
  o += panel(566, 394, 'indigo', '人在回路 · 助教复核', 'HUMAN-IN-THE-LOOP');
  const hy = [616, 700], hh = 58;
  o += card({ x: IX, y: hy[0], w: IW, h: hh, c: 'indigo', tSize: 16, sSize: 12.5, title: '复核队列', sub: ['验证失败 → 无证据 → 疑似粗心 → 人工 优先级排队'] });
  o += arrow(`M${IX + IW / 2} ${hy[0] + hh + 3} V${hy[1] - 4}`, 'indigo');
  o += card({ x: IX, y: hy[1], w: IW, h: hh, c: 'indigo', tSize: 16, sSize: 12.5, title: '助教确认 / 改判', sub: ['题目 → 学生答案 → AI 判定 一屏对照'] });
  const oy = 800, oh = 66, ow = 236;
  o += card({ x: IX, y: oy, w: ow, h: oh, c: 'green', tSize: 15.5, sSize: 12.5, title: '回写掌握度与错因', sub: ['改判即时生效'] });
  o += card({ x: IX + ow + 20, y: oy, w: PX + PW - 22 - (IX + ow + 20), h: oh, c: 'cyan', tSize: 15.5, sSize: 12.5, title: '导出偏好数据', sub: ['对齐教师判断'] });
  o += arrow(`M${IX + ow / 2} ${hy[1] + hh + 3} V${oy - 4}`, 'indigo');
  o += arrow(`M${IX + ow + 20 + 80} ${hy[1] + hh + 3} V${oy - 4}`, 'indigo');
  o += text(PX + PW / 2, 910, '学生也可在问答、解题、错题本中一键「请助教看看」', { size: 13, color: SUB, anchor: 'middle' });
  o += text(PX + PW / 2, 934, 'AI 负责规模化，教师负责关键判断', { size: 13, color: C.indigo.main, anchor: 'middle', weight: 600 });
  // 验证 → 复核队列
  const ver = boxes[4];
  o += arrow(`M${f(ver.x + ver.w + 4)} ${hy[0] + hh / 2} H${IX - 4}`, 'indigo');
  o += tag(1279, hy[0] + hh / 2 - 17, '入队', 'indigo', 12);
  // 回写掌握度 → 掌握度节点
  const mas = boxes[5];
  o += arrow(`M${IX - 4} ${oy + oh / 2} H${f(mas.x + mas.w + 6)}`, 'green');
  o += tag(1165, oy + oh / 2, '回写', 'green');
  // 偏好数据 → 训练数据（右侧通道）
  const px = PX + PW - 30;
  o += arrow(`M${px - 30} ${oy - 2} V${oy - 14} H${px} V${fy[2] + fh / 2} H${IX + IW + 4}`, 'cyan', { dashed: true });

  return frame('智能学习闭环与数据流', '一次学习交互如何流经各组件：实时闭环负责「教对」，人在回路负责「判准」，数据飞轮负责「越用越好」', o, [
    { kind: 'line', c: 'indigo', label: '实时学习闭环' },
    { kind: 'line', c: 'cyan', dashed: true, label: '数据飞轮回流' },
    { kind: 'box', c: 'amber', strong: true, label: '可信验证节点' },
  ]);
}

/* ───────────────────────── 图 3：数据与模型生产管线 ───────────────────────── */
function d3() {
  let o = '';
  const X = 60, W = 1800, LW = 170, CX = 250, CW = 1846 - CX, cw = 226, gap = (CW - cw * 6) / 5;
  const colX = i => CX + i * (cw + gap);
  const lane = (y, h, c, num, label, en) => {
    const k = C[c];
    let s = rect(X, y, W, h, { fill: k.soft, stroke: k.line, sw: 1.2, rx: 14 });
    s += rect(X, y, LW, h, { fill: k.main, rx: 14 }) + rect(X + LW - 14, y, 14, h, { fill: k.main, rx: 0 });
    s += text(X + LW / 2, y + h / 2 - 22, num, { size: 15, weight: 700, color: '#fff', anchor: 'middle', opacity: 0.7 });
    s += text(X + LW / 2, y + h / 2 + 8, label, { size: 22, weight: 700, color: '#fff', anchor: 'middle' });
    s += text(X + LW / 2, y + h / 2 + 30, en, { size: 11, color: '#fff', anchor: 'middle', opacity: 0.75 });
    return s;
  };
  const flow = (y, c, items) => {
    let s = '';
    const h = 150, cy = y + 40;
    items.forEach((it, i) => {
      s += card({ x: colX(i), y: cy, w: cw, h, c: it.c || c, tSize: 18, sSize: 13.5, ...it });
      if (i < items.length - 1) s += arrow(`M${f(colX(i) + cw + 5)} ${cy + h / 2} H${f(colX(i + 1) - 6)}`, c, { dashed: !!items[i + 1].dashed && i + 1 === items.length - 1 });
    });
    return s;
  };

  const L1 = 150, L2 = 412, B = 660, L3 = 768, LH = 230;
  o += lane(L1, LH, 'blue', '01', '语料入库', 'CORPUS INGEST');
  o += flow(L1, 'blue', [
    { title: '多格式原始资料', sub: ['教材 · 课件 · 习题', 'PDF · PPTX · DOCX · TXT', '86 份 · 557 MB'] },
    { title: '解析 + OCR 回退', sub: ['PyMuPDF 版式解析', 'DOCX 保留表格顺序', '扫描件 → 视觉大模型'] },
    { title: '4 种分块策略', sub: ['章节结构切分', '课件按页聚合', '标题层级 · 重叠窗口'] },
    { title: '内容哈希去重', sub: ['sha256（课程 + 文本）', '跨批次去重', '修正「内容对、出处错」'] },
    { title: '检索索引', sub: ['FTS5 Trigram + BM25', '课程隔离 · 带页码元数据', 'bge-m3 向量索引'] },
    { big: '2646 块', title: '教材级检索语料', sub: ['100% 带真实页码', '引用命中率 92.9%'], strong: true },
  ]);

  o += lane(L2, LH, 'indigo', '02', '题库生产', 'QUESTION BANK');
  o += flow(L2, 'indigo', [
    { title: '三路题源', sub: ['真题 OCR 数字化', '练习题三层清洗', 'AI 检索教材生成'] },
    { title: '知识点对齐', sub: ['别名→考纲→包含→相似→章节', '五级匹配', '零「未分类」题目'] },
    { title: '确定性验证', sub: ['代码跑测试 · 数值自洽', '按 4 类原因定向重试', '不通过不入库'], c: 'amber', strong: true },
    { title: '三模型交叉复核', sub: ['三家模型独立盲做', '一票否决', '拦截 30% 隐性坏题'], c: 'amber' },
    { title: '入库与组卷', sub: ['题干去重 · 附教材出处', '最大余额法按分值分题', '章节轮转 · 真题优先'] },
    { big: '4719 题', title: '408 四门题库', sub: ['40 套零重复模考卷', '90% 生成题带出处'], strong: true },
  ]);

  // 统一验证器带
  {
    const k = C.amber;
    o += rect(X, B, W, 90, { fill: k.tint, stroke: k.main, sw: 2, rx: 14 });
    o += rect(X, B, LW, 90, { fill: k.main, rx: 14 }) + rect(X + LW - 14, B, 14, 90, { fill: k.main, rx: 0 });
    o += text(X + LW / 2, B + 45, '统一验证器', { size: 21, weight: 700, color: '#fff', anchor: 'middle' });
    o += text(X + LW / 2, B + 66, 'VERIFIERS', { size: 11, color: '#fff', anchor: 'middle', opacity: 0.8 });
    ['代码沙箱', '数值规范判分', '得分点规则', 'AST 复杂度'].forEach((s, i) => {
      o += rect(CX + 10 + i * 150, B + 27, 138, 36, { fill: '#fff', stroke: k.line, sw: 1.3, rx: 8 });
      o += text(CX + 10 + i * 150 + 69, B + 50, s, { size: 15, weight: 600, anchor: 'middle' });
    });
    o += text(870, B + 40, '同一套确定性验证器，服务四个场景', { size: 18, weight: 700, color: k.main });
    o += text(870, B + 68, '在线判题  ·  题库入库  ·  训练样本过滤  ·  强化学习奖励   —— 正确性由规则保证，而非模型自评', { size: 15, color: INK });
    // 接入点
    const dot = (x, y) => `<circle cx="${f(x)}" cy="${y}" r="5" fill="${k.main}"/>`;
    const v1 = colX(2) + cw / 2;
    o += `<path d="M${f(v1)} ${L2 + 40 + 150} V${B}" stroke="${k.main}" stroke-width="2.5"/>` + dot(v1, B);
    [1, 3].forEach(i => {
      const v = colX(i) + cw / 2;
      o += `<path d="M${f(v)} ${B + 90} V${L3 + 40}" stroke="${k.main}" stroke-width="2.5"/>` + dot(v, B + 90);
    });
  }

  o += lane(L3, LH, 'cyan', '03', '模型训练', 'MODEL TRAINING');
  o += flow(L3, 'cyan', [
    { title: '教师模型蒸馏', sub: ['解题 · 诊断 · 问答 · 出题', '四类任务样本', '3000 条批量合成'] },
    { title: '验证器过滤', sub: ['只保留通过验证的样本', '盲审抽检 50/50 通过'], c: 'amber', strong: true },
    { title: 'SFT 监督微调', sub: ['Qwen3-14B · QLoRA', '8899 条训练样本', 'unsloth 加速'] },
    { title: 'GRPO 强化学习', sub: ['RLVR 可验证奖励', '奖励复用平台验证器', '每题 6 个采样'], c: 'amber' },
    { title: '多 LoRA 服务', sub: ['vLLM 单基座多适配器', '按任务别名路由', 'OpenAI 兼容接口'] },
    { big: '统一编排', title: '接入平台模型层', sub: ['任务级路由按任务分派', '与云端模型协同'] },
  ]);

  // 语料 → 题库：检索教材片段
  {
    const sx = colX(4) + cw / 2, tx = colX(0) + cw / 2, gy = (L1 + LH + L2) / 2;
    o += arrow(`M${f(sx)} ${L1 + 40 + 150 + 3} V${gy} H${f(tx)} V${L2 + 40 - 5}`, 'blue');
    o += tag((sx + tx) / 2, gy, '检索教材片段 · 出题附带页码出处', 'blue', 13);
  }
  return frame('数据与模型生产管线', '语料、题库、模型三条离线管线，由同一套确定性验证器串联——这是平台「可信」的根基', o, [
    { kind: 'line', c: 'indigo', label: '数据流' },
    { kind: 'box', c: 'amber', strong: true, label: '验证器接入点' },
  ]);
}

fs.mkdirSync(OUT, { recursive: true });
fs.writeFileSync(path.join(OUT, '01-分层总览.svg'), d1());
fs.writeFileSync(path.join(OUT, '02-学习闭环.svg'), d2());
fs.writeFileSync(path.join(OUT, '03-生产管线.svg'), d3());
console.log('svg written');
