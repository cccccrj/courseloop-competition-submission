"""SFT 数据校验（训练前 gate）：扫描 data/sft/<course>/{solve,diagnose,practice}.jsonl，
逐条核查 assistant JSON 合法性 / 必填字段 / canonical 可判性，输出 course×intent 质量报告。

判定项：
- json_ok：assistant 能解析为 JSON。
- fields_ok：该 intent 必填字段齐全（见 REQUIRED）。
- canonical_ok：非代码题的 final_answer_canonical 是 numeric_checker 认识的 kind（能机器判分）。
  —— 抓 teacher 偶发的非法 kind（如 'nested'）等不可判样本。
- 汇总合法率，并可 --clean 输出剔除坏样本后的 *.clean.jsonl。

用法（rag_env）：
    python -m scripts.validate_sft                 # 全量报告
    python -m scripts.validate_sft --course co     # 只看某门
    python -m scripts.validate_sft --clean         # 额外产出 .clean.jsonl（坏样本剔除）
"""
from __future__ import annotations

import argparse
import glob
import json
import os

from backend.core.courses import get_course

SFT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "sft"))
VALID_KINDS = {"scalar", "percent", "int", "choice", "bits", "ipv4net", "set"}

REQUIRED = {
    "solve": ["problem_type", "target", "steps", "final_answer"],
    "diagnose": ["correct", "first_wrong_step", "error_tags", "diagnosis"],
    "practice": ["stem", "type", "difficulty"],
}


def canonical_ok(obj: dict, code_enabled: bool) -> bool:
    """代码门不看 canonical；非代码门若含 canonical 必须是可判 kind。无 canonical 但有 rubric 也算 OK。"""
    if code_enabled:
        return True
    canon = obj.get("final_answer_canonical")
    if canon is None:
        return bool(obj.get("rubric")) or True  # solve/diagnose 未必带 canonical，不强制
    if not isinstance(canon, dict):
        return False
    return canon.get("kind") in VALID_KINDS


def check_line(line: str, intent: str, code_enabled: bool) -> tuple[bool, bool, bool]:
    try:
        row = json.loads(line)
        obj = json.loads(row["messages"][-1]["content"])
    except (json.JSONDecodeError, KeyError, IndexError):
        return False, False, False
    fields_ok = all(k in obj for k in REQUIRED.get(intent, []))
    canon = canonical_ok(obj, code_enabled)
    return True, fields_ok, canon


def validate_course(course: str, do_clean: bool) -> dict:
    c = get_course(course)
    report = {}
    for intent in ("solve", "diagnose", "practice"):
        path = os.path.join(SFT_DIR, course, f"{intent}.jsonl")
        if not os.path.exists(path):
            continue
        total = json_ok = fields_ok = canon_ok = 0
        clean_rows = []
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            total += 1
            j, f, cn = check_line(line, intent, c.code_enabled)
            json_ok += j
            fields_ok += f
            canon_ok += cn
            if j and f and cn:
                clean_rows.append(line)
        report[intent] = {"total": total, "json_ok": json_ok,
                          "fields_ok": fields_ok, "canonical_ok": canon_ok,
                          "clean": len(clean_rows)}
        if do_clean and clean_rows:
            with open(path.replace(".jsonl", ".clean.jsonl"), "w", encoding="utf-8") as f:
                f.write("\n".join(clean_rows) + "\n")
    return report


def main():
    ap = argparse.ArgumentParser(description="SFT 数据校验（训练前 gate）")
    ap.add_argument("--course", default="", help="只看某门；缺省全部")
    ap.add_argument("--clean", action="store_true", help="产出剔除坏样本的 .clean.jsonl")
    args = ap.parse_args()

    courses = [args.course] if args.course else \
        sorted({os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(SFT_DIR, "*", "*.jsonl"))
                if not p.endswith(".clean.jsonl")})
    grand = {"total": 0, "clean": 0}
    for course in courses:
        rep = validate_course(course, args.clean)
        if not rep:
            continue
        print(f"\n=== {course} ===")
        for intent, s in rep.items():
            t = s["total"] or 1
            print(f"  {intent:9s} 总{s['total']:5d} | JSON合法 {s['json_ok']/t*100:5.1f}% "
                  f"| 字段完整 {s['fields_ok']/t*100:5.1f}% | canonical可判 {s['canonical_ok']/t*100:5.1f}% "
                  f"| 干净 {s['clean']}")
            grand["total"] += s["total"]
            grand["clean"] += s["clean"]
    gt = grand["total"] or 1
    print(f"\n合计 {grand['total']} 条，干净 {grand['clean']}（{grand['clean']/gt*100:.1f}%）")


if __name__ == "__main__":
    main()
