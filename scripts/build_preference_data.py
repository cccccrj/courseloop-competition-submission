"""把助教的改判记录导成偏好（DPO）数据。

## 这些数据本来就在库里

`db.review_error_record` 在助教改判时会把 AI 的原判存进 `ai_error_type`，
把人工的正确判定写进 `error_type`。这就是一组现成的偏好对：

    (题目 + 学生作答, 人工判对的标签 = chosen, AI 判错的标签 = rejected)

在此之前只有 `/teaching/overview` 把它读出来显示在页面上，没有任何脚本消费
（docs/18 §2.1①）。本脚本把它导出来，补上这条回流。

## 它的价值在叙事，不在量

改判记录不会多。几十条数据训不出什么，但"人在环的判定会回流改进模型"这句话
需要一个能跑的脚本和一份真实的文件来支撑，而不是一张示意图。
所以本脚本**只做导出，不做训练**，也不做任何数据增广 —— 编出来的偏好对
比没有偏好对更糟。

## 丢弃规则（宁缺毋滥）

- `verdict != 'override'`：确认判定（confirm）说明 AI 判对了，不构成偏好对。
  用 `--include-confirm` 可以把它们导成"chosen == AI 判定"的正样本，默认不导。
- 找不回学生作答的记录一律丢弃。没有输入的偏好对是残缺的，
  补一句"（作答缺失）"就是在造数据。
- chosen == rejected 的丢弃：改判后标签没变（助教只改了备注）不算偏好。

用法：
    python -m scripts.build_preference_data                    # 干跑，只报数
    python -m scripts.build_preference_data --out data/preference/diagnose_dpo.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter

from backend.agents.error_diagnoser import build_messages
from backend.core import db
from backend.learning.error_taxonomy import name_of

DEFAULT_OUT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "data", "preference", "diagnose_dpo.jsonl"))


def _label(code: str, note: str = "") -> str:
    """偏好对的两侧写成同一形状的 JSON，只在错因标签上有差别。

    直接拿一个裸标签当 chosen/rejected 会让偏好落在"格式"而不是"判断"上 ——
    模型学到的会是"输出一个词"，而线上要的是完整的诊断 JSON。
    """
    payload = {"error_tags": [code], "error_tag_names": [name_of(code)]}
    if note:
        payload["remediation"] = note
    return json.dumps(payload, ensure_ascii=False)


def build(include_confirm: bool = False, limit: int = 1000) -> tuple[list[dict], Counter]:
    stat: Counter = Counter()
    out: list[dict] = []
    for row in db.list_reviewed_errors(limit=limit):
        stat["reviewed"] += 1
        verdict = row.get("ta_verdict") or ""
        ai_code = row.get("ai_error_type") or ""
        human_code = row.get("error_type") or ""

        if verdict != "override":
            stat["skip_confirm"] += 1
            if not include_confirm:
                continue
            ai_code = human_code          # 确认即"AI 判对了"，两侧同标签
        if not human_code:
            stat["skip_no_label"] += 1
            continue
        if verdict == "override" and (not ai_code or ai_code == human_code):
            # 改判后标签没变：助教只改了备注，不构成偏好
            stat["skip_same_label"] += 1
            continue

        student_answer = (row.get("student_answer") or "").strip()
        stem = (row.get("stem") or "").strip()
        if not stem:
            # 拍照批改/自由问答记的错题没有题号，题干只能从 diagnosis 前缀拿
            stem = (row.get("diagnosis") or "").split(" || ")[0].strip()
        if not student_answer or not stem:
            stat["skip_no_input"] += 1
            continue

        messages, _ = build_messages(stem, student_answer,
                                     reference=str(row.get("reference") or ""),
                                     course=row.get("course_id") or "ds-algo")
        out.append({
            "record_id": row["id"],
            "course": row.get("course_id") or "",
            "knowledge": row.get("knowledge") or "",
            "prompt": messages,
            "chosen": _label(human_code, (row.get("ta_note") or "").strip()),
            "rejected": _label(ai_code),
            "meta": {"verdict": verdict, "ta": row.get("ta_username") or "",
                     "reviewed_at": row.get("ta_reviewed_at") or "",
                     "ai_error_name": name_of(ai_code), "human_error_name": name_of(human_code)},
        })
        stat["kept"] += 1
        stat[f"pair:{ai_code}->{human_code}"] += 1
    return out, stat


def main() -> int:
    ap = argparse.ArgumentParser(description="把助教改判记录导成 DPO 偏好数据（默认干跑）")
    ap.add_argument("--out", default="", help="输出 JSONL 路径；不给就只报数不写文件")
    ap.add_argument("--include-confirm", action="store_true",
                    help="把「确认判定」也导成 chosen==rejected 的正样本")
    ap.add_argument("--limit", type=int, default=1000)
    args = ap.parse_args()

    db.init_db()
    rows, stat = build(include_confirm=args.include_confirm, limit=args.limit)

    print(f"▶ 复核记录 {stat['reviewed']} 条 → 可用偏好对 {stat['kept']} 条")
    for key, label in (("skip_confirm", "确认判定（AI 判对，非偏好）"),
                       ("skip_same_label", "改判后标签未变"),
                       ("skip_no_label", "没有人工标签"),
                       ("skip_no_input", "找不回学生作答或题干")):
        if stat[key]:
            print(f"  丢弃 {stat[key]:>4} 条：{label}")
    pairs = [(k.split(":", 1)[1], v) for k, v in stat.items() if k.startswith("pair:")]
    if pairs:
        print("  改判方向（AI 原判 → 人工判定）：")
        for name, n in sorted(pairs, key=lambda x: -x[1])[:10]:
            src, dst = name.split("->")
            print(f"    {name_of(src)} → {name_of(dst)}：{n} 次")

    if not args.out:
        print("（干跑，未写文件。加 --out <path> 导出）")
        return 0
    if not rows:
        # 空文件比没有文件更容易被误当成"跑过了、就是没数据"
        print("没有可用的偏好对，不写空文件。")
        return 1
    path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"✓ 已写入 {path}（{len(rows)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
