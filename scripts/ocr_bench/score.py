"""横评打分 + 成本核算 → docs/10-OCR方案横评.md。

自动指标(可靠)：印刷忠实度(printed_must 子串命中率)、手写答案准确率(choice 精确匹配)、幻觉(forbidden 出现)。
LLM-judge(deepseek-v3)：结构质量(阅读顺序/表格/公式)holistic 0-1。
成本：读各方案 _meta.json 的 cost/latency + judge 自身成本。
PP-OCR 基线：读 data/ocr/_ocr_probe/page_00N_res.json（覆盖 p1/p2/p3_2021）。
"""
from __future__ import annotations

import glob
import json
import os
import re

import httpx
from openai import OpenAI

from scripts.ocr_bench import common
from scripts.ocr_bench.prices import cost_of

PPOCR_MAP = {"p1_2021_choice": 1, "p2_2021_table": 2, "p3_2021_riscv": 3}
JUDGE_MODEL = "deepseek-v3"
WEIGHTS = {"printed": 0.30, "answers": 0.30, "structure": 0.20, "no_halluc": 0.20}


def ppocr_text(pid: str) -> str | None:
    n = PPOCR_MAP.get(pid)
    if not n:
        return None
    jf = os.path.join(common.ROOT, "data", "ocr", "_ocr_probe", f"page_{n:03d}_res.json")
    if not os.path.exists(jf):
        return None
    d = json.load(open(jf))
    return "\n".join(d.get("rec_texts", []))


def load_md(method: str, pid: str) -> str | None:
    if method == "pp-ocr":
        return ppocr_text(pid)
    p = os.path.join(common.OUT, method, f"{pid}.md")
    return open(p, encoding="utf-8").read() if os.path.exists(p) else None


def parse_choice_answers(method: str, pid: str) -> dict:
    if method == "pp-ocr":
        return {}
    p = os.path.join(common.OUT, method, f"{pid}.answers.json")
    if not os.path.exists(p):
        return {}
    txt = open(p, encoding="utf-8").read()
    # 先试完整 JSON;失败(如被 max_tokens 截断)再正则兜底抽 "题号":"字母"
    m = re.search(r"\{.*\}", txt, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            ch = obj.get("选择题") or obj.get("choices") or {}
            if ch:
                return {str(k): str(v).strip().upper()[:1] for k, v in ch.items() if v}
        except json.JSONDecodeError:
            pass
    # 兜底:抓 "1": "B" 形式(容忍截断/多余文本),只取 A-D
    out = {}
    for q, a in re.findall(r'"(\d+)"\s*:\s*"([A-Da-d])"', txt):
        out.setdefault(q, a.upper())
    return out


def score_printed(md: str, must: list[str]) -> tuple[float, int]:
    if not md:
        return 0.0, 0
    norm = md.replace(" ", "").replace("　", "")
    hit = sum(1 for s in must if s.replace(" ", "") in norm)
    return hit / max(1, len(must)), hit


def score_answers(pred: dict, gt: dict) -> tuple[float, str]:
    if not gt:
        return -1.0, "N/A"
    ok = sum(1 for q, a in gt.items() if pred.get(q) == a)
    detail = " ".join(f"{q}:{pred.get(q,'-')}{'✓' if pred.get(q)==a else '✗('+a+')'}" for q, a in gt.items())
    return ok / len(gt), detail


def score_halluc(md: str, forbidden: list[str]) -> tuple[float, list[str]]:
    if not md or not forbidden:
        return 1.0, []
    hit = [f for f in forbidden if f.replace(" ", "") in md.replace(" ", "")]
    return (1.0 if not hit else max(0.0, 1 - 0.5 * len(hit))), hit


def judge_structure(cli, md: str, hint: str) -> tuple[float, dict]:
    if not md:
        return 0.0, {"cost": 0}
    prompt = (f"评估一段扫描试卷 OCR/转写结果的【结构质量】,0-1 打分。评分要点:{hint}\n"
              "维度:阅读顺序是否合理、表格是否结构化、公式是否规范。\n"
              f"待评文本(截断):\n{md[:2500]}\n\n"
              '只输出JSON:{"reading_order":0-1,"tables":0-1,"formulas":0-1,"overall":0-1,"note":"一句话"}')
    try:
        r = cli.chat.completions.create(model=JUDGE_MODEL, max_tokens=300, temperature=0,
                                        messages=[{"role": "user", "content": prompt}])
        u = r.usage
        cost = cost_of(JUDGE_MODEL, u.prompt_tokens, u.completion_tokens) if u else 0
        m = re.search(r"\{.*\}", r.choices[0].message.content, re.S)
        obj = json.loads(m.group(0)) if m else {}
        return float(obj.get("overall", 0)), {**obj, "cost": round(cost, 4)}
    except Exception as e:  # noqa: BLE001
        return 0.0, {"error": str(e)[:60], "cost": 0}


def main():
    gt = common.load_gt()
    methods = sorted([os.path.basename(d) for d in glob.glob(os.path.join(common.OUT, "*")) if os.path.isdir(d)])
    if os.path.exists(os.path.join(common.ROOT, "data", "ocr", "_ocr_probe")):
        methods = ["pp-ocr"] + methods
    from backend.core.config import settings
    cli = OpenAI(base_url="https://api.zhizengzeng.com/v1", api_key=settings.llm_api_key,
                 http_client=httpx.Client(trust_env=False, timeout=60))

    rows = {}
    judge_cost = 0.0
    for method in methods:
        agg = {"printed": [], "answers": [], "structure": [], "no_halluc": [], "halluc_items": []}
        for _, _, pid in common.TEST_PAGES:
            g = gt.get(pid, {})
            md = load_md(method, pid)
            sp, _ = score_printed(md, g.get("printed_must", []))
            sa, _ = score_answers(parse_choice_answers(method, pid), g.get("choice_answers", {}))
            sh, hitems = score_halluc(md, g.get("halluc_forbidden", []))
            ss, jinfo = judge_structure(cli, md, g.get("structure_hint", ""))
            judge_cost += jinfo.get("cost", 0)
            agg["printed"].append(sp)
            if sa >= 0:
                agg["answers"].append(sa)
            agg["structure"].append(ss)
            agg["no_halluc"].append(sh)
            agg["halluc_items"] += hitems
        def avg(x):
            return sum(x) / len(x) if x else 0.0
        # 成本/延迟
        meta_p = os.path.join(common.OUT, method, "_meta.json")
        cost = lat = 0.0
        if os.path.exists(meta_p):
            meta = json.load(open(meta_p))
            cost = sum(v.get("cost_rmb", 0) for v in meta.values())
            lats = [v.get("latency_s", 0) for v in meta.values() if v.get("latency_s")]
            lat = sum(lats) / len(lats) if lats else 0
        pr, an, st, nh = avg(agg["printed"]), (avg(agg["answers"]) if agg["answers"] else None), avg(agg["structure"]), avg(agg["no_halluc"])
        comp = (WEIGHTS["printed"] * pr + WEIGHTS["answers"] * (an if an is not None else pr)
                + WEIGHTS["structure"] * st + WEIGHTS["no_halluc"] * nh)
        rows[method] = {"printed": pr, "answers": an, "structure": st, "no_halluc": nh,
                        "comp": comp, "cost": cost, "latency": lat,
                        "halluc": agg["halluc_items"], "local": method in {"pp-ocr", "pp-structurev3", "mineru", "got-ocr2"}}

    # 报告
    lines = ["# 10 · OCR 方案横评结果\n",
             f"> 测试集:{[p[2] for p in common.TEST_PAGES]} | 评分权重 {WEIGHTS} | judge=deepseek-v3\n",
             "## 打分矩阵\n",
             "| 方案 | 综合分 | 印刷忠实 | 手写答案 | 结构 | 无幻觉 | ¥/全测试集 | 平均延迟s | 类型 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for m, r in sorted(rows.items(), key=lambda x: -x[1]["comp"]):
        an = f"{r['answers']*100:.0f}%" if r["answers"] is not None else "—"
        typ = "本地(免费)" if r["local"] else "API(计费)"
        lines.append(f"| {m} | **{r['comp']:.2f}** | {r['printed']*100:.0f}% | {an} | {r['structure']:.2f} | {r['no_halluc']:.2f} | {r['cost']:.3f} | {r['latency']:.0f} | {typ} |")
    lines.append(f"\n> API 组总成本 ¥{sum(r['cost'] for r in rows.values()):.3f} + judge ¥{judge_cost:.3f}\n")
    lines.append("## 幻觉记录\n")
    for m, r in rows.items():
        if r["halluc"]:
            lines.append(f"- **{m}**: 编造 {r['halluc']}")
    common.save_json(os.path.join(common.OUT, "_scores.json"), rows)
    doc = os.path.join(common.ROOT, "docs", "10-OCR方案横评.md")
    open(doc, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("打分完成 →", doc)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
