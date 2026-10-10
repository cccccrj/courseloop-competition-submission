"""CourseLoop 离线评测脚本（固定评测集 → 量化指标）。

复用真实链路（不 mock）：
    - 教材问答：rag/retriever.retrieve      → 引用命中率 / 来源命中率 / 检索空率
    - 错因诊断：agents/error_diagnoser.diagnose → 首错定位准确率 / 错因标签一致率
    - 解题判定：verification 沙箱跑题库参考解  → 沙箱通过率（答案一致性基线，应 100%）

用法（务必用 rag_env）：
    rag_env python -m scripts.evaluate                # 全部
    rag_env python -m scripts.evaluate --only qa      # 只跑教材问答
    rag_env python -m scripts.evaluate --only diagnose
    rag_env python -m scripts.evaluate --only solve
    rag_env python -m scripts.evaluate --with-solver  # solve 额外跑 LLM solver 比对答案（慢、耗 token）

输出：终端指标表 + data/eval/report_<workhorse>__<reasoner>.json（含模型名，便于日后本地 vs 云端对比）。
"""
from __future__ import annotations

import argparse
import json
import os
import time

from backend.core.config import settings

EVAL_DIR = os.path.abspath(os.path.join(settings.raw_dir, "..", "eval"))


def _load(name: str) -> dict:
    with open(os.path.join(EVAL_DIR, name), encoding="utf-8") as f:
        return json.load(f)


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


# ---------- 教材问答：引用命中率 ----------
def eval_qa() -> dict:
    from backend.rag.retriever import get_engine
    engine = get_engine()
    if not engine.has_corpus():
        print("  [跳过] 向量库为空，先运行 rag_env python -m backend.rag.ingest")
        return {"skipped": "no_corpus"}
    cases = _load("qa_cases.json")["cases"]
    cite_hit = src_hit = nonempty = 0
    rows = []
    per_course: dict[str, dict[str, int]] = {}
    for c in cases:
        # 用例可带 course / doc_types：四门里只有数据结构有电子教材，
        # co/os/net 的语料全是课件与习题，仍按 textbook 过滤会一条都检不出来。
        course = c.get("course") or "ds-algo"
        doc_types = c.get("doc_types") or ["textbook", "courseware", "exercise"]
        hits = engine.retrieve(c["question"], doc_types=doc_types, top_n=3, course=course)
        kw, src = c["expect_chapter_kw"], c["expect_source"]
        has_src = any(h.source == src for h in hits)
        has_cite = any(h.source == src and (kw in h.title_path or kw in h.text) for h in hits)
        nonempty += 1 if hits else 0
        src_hit += 1 if has_src else 0
        cite_hit += 1 if has_cite else 0
        agg = per_course.setdefault(course, {"n": 0, "cite": 0, "src": 0})
        agg["n"] += 1
        agg["cite"] += 1 if has_cite else 0
        agg["src"] += 1 if has_src else 0
        rows.append((course, c["question"][:26], "✓" if has_cite else ("~src" if has_src else "✗")))
    n = len(cases)
    m = {"total": n, "citation_hit_rate": _pct(cite_hit, n),
         "source_hit_rate": _pct(src_hit, n), "retrieval_nonempty_rate": _pct(nonempty, n),
         "by_course": {k: {"total": v["n"], "citation_hit_rate": _pct(v["cite"], v["n"]),
                           "source_hit_rate": _pct(v["src"], v["n"])}
                       for k, v in sorted(per_course.items())}}
    print(f"\n[教材问答] {n} 题")
    for course, q, r in rows:
        print(f"    {r:>4}  [{course}] {q}")
    for course, v in m["by_course"].items():
        print(f"  · {course}: {v['total']} 题，引用命中 {v['citation_hit_rate']}% / "
              f"来源命中 {v['source_hit_rate']}%")
    print(f"  引用命中率 {m['citation_hit_rate']}% | 来源命中率 {m['source_hit_rate']}% | "
          f"检索非空率 {m['retrieval_nonempty_rate']}%")
    return m


# ---------- 错因诊断：首错定位 + 错因一致 ----------
def eval_diagnose() -> dict:
    if not settings.text_llm_ready:
        print("  [跳过] 未配置 LLM，诊断评测需要 LLM")
        return {"skipped": "no_llm"}
    from backend.agents import error_diagnoser as diag
    cases = _load("diagnose_cases.json")["cases"]
    step_ok = tag_ok = parsed_ok = 0
    rows = []
    for c in cases:
        d = diag.diagnose(c["problem"], c["student_answer"], reference=c.get("reference", ""))
        if d.get("error"):
            rows.append((c["problem"][:24], "解析失败", "", ""))
            continue
        parsed_ok += 1
        pred_step = d.get("first_wrong_step")
        tags = d.get("error_tags") or []
        s_ok = (pred_step == c["gold_first_wrong_step"])
        t_ok = c["gold_error_tag"] in tags
        step_ok += 1 if s_ok else 0
        tag_ok += 1 if t_ok else 0
        rows.append((c["problem"][:24],
                     f"step {pred_step}/{c['gold_first_wrong_step']}{'✓' if s_ok else '✗'}",
                     f"{','.join(tags[:2])}", "✓" if t_ok else "✗"))
    n = len(cases)
    m = {"total": n, "json_parse_rate": _pct(parsed_ok, n),
         "first_wrong_step_acc": _pct(step_ok, n), "error_tag_consistency": _pct(tag_ok, n)}
    print(f"\n[错因诊断] {n} 题")
    for prob, s, tags, t in rows:
        print(f"    {prob:<26} {s:<16} tag[{t}] {tags}")
    print(f"  首错定位准确率 {m['first_wrong_step_acc']}% | 错因标签一致率 {m['error_tag_consistency']}% | "
          f"JSON 解析率 {m['json_parse_rate']}%")
    return m


# ---------- 解题：题库参考解沙箱通过率（答案一致性基线） ----------
def eval_solve(with_solver: bool = False) -> dict:
    from backend.core import db
    from backend.verification.unit_test_runner import run_python_tests
    probs = db.list_problems(limit=200)
    code_total = sand_pass = 0
    solver_total = solver_match = 0
    for p in probs:
        full = db.get_problem(p["id"], with_solution=True)
        if full["verification_type"] == "unit_test" and full.get("test_cases") and full.get("ref_code"):
            code_total += 1
            vr = run_python_tests(full["ref_code"], full["func_name"], full["test_cases"])
            sand_pass += 1 if vr.passed else 0
            if not vr.passed:
                print(f"    ✗ 参考解未过沙箱: {full['stem'][:30]} -> {vr.details}")
            if with_solver:
                from backend.agents import solver as solver_mod
                sol = solver_mod.solve(full["stem"])
                fa = str(sol.get("final_answer", "")).strip().lower().replace(" ", "")
                ref = str(full.get("answer", "")).strip().lower().replace(" ", "")
                solver_total += 1
                solver_match += 1 if (fa and ref and (fa in ref or ref in fa)) else 0
    m = {"code_problems": code_total, "sandbox_pass_rate": _pct(sand_pass, code_total)}
    if with_solver:
        m["solver_total"] = solver_total
        m["solver_answer_consistency"] = _pct(solver_match, solver_total)
    print(f"\n[解题·题库] 代码题 {code_total} 道")
    print(f"  参考解沙箱通过率 {m['sandbox_pass_rate']}%（答案一致性基线，应=100%）")
    if with_solver:
        print(f"  LLM solver 答案一致率 {m['solver_answer_consistency']}%（{solver_match}/{solver_total}）")
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["qa", "diagnose", "solve"], default=None)
    ap.add_argument("--with-solver", action="store_true", help="solve 额外跑 LLM solver 比对答案（慢）")
    args = ap.parse_args()

    print("=" * 64)
    print("CourseLoop 离线评测")
    print(f"  workhorse={settings.workhorse_model or '(未配置)'} | reasoner={settings.reasoner_model or '(未配置)'}")
    print(f"  device={settings.device} | llm_ready={settings.text_llm_ready}")
    print("=" * 64)

    report: dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "workhorse_model": settings.workhorse_model,
                    "reasoner_model": settings.reasoner_model,
                    "device": settings.device, "metrics": {}}

    if args.only in (None, "qa"):
        report["metrics"]["qa"] = eval_qa()
    if args.only in (None, "diagnose"):
        report["metrics"]["diagnose"] = eval_diagnose()
    if args.only in (None, "solve"):
        report["metrics"]["solve"] = eval_solve(with_solver=args.with_solver)

    os.makedirs(EVAL_DIR, exist_ok=True)

    def _safe(s: str) -> str:
        return (s or "none").replace("/", "_").replace(":", "_")

    out = os.path.join(EVAL_DIR, f"report_{_safe(settings.workhorse_model)}__{_safe(settings.reasoner_model)}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n报告已写入：{out}")


if __name__ == "__main__":
    main()
