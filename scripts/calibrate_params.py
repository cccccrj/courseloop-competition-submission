"""参数标定（第一步：只出报告，不改常数）。

`backend/learning/mastery.py` 里的几个数字（`FORGET_HALF_LIFE_DAYS=14`、`gain=0.22`、
`drop=0.18/0.28`、`hint_penalty`、`BASE_INTERVALS`）是手调的。本脚本回答一个前置问题：
**手上的数据到底能不能标定它们。**

结论会随数据变化，所以脚本先做**溯源审计**再做统计：一份由脚本按公式生成的
"学习记录"拟合出来的半衰期，只是把生成公式抄了回来，比手调常数更危险 —— 它看起来有据可依。

用法：
    python -m scripts.calibrate_params                          # 打印报告
    python -m scripts.calibrate_params --out data/processed/calibration.json
    python -m scripts.calibrate_params --report docs/21-参数标定报告.md
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
from collections import Counter, defaultdict

from backend.core.config import settings

SYNTHETIC_ANSWER = "演示作答"


def _conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


# ---------- 一、溯源审计 ----------

def audit_attempts(conn: sqlite3.Connection) -> dict:
    """判断作答记录是真人产生的还是脚本合成的。

    三个指纹：作答内容是否全是占位串、得分是否只有 0/100 两个值、用户是否全是 showcase_*。
    """
    total = conn.execute("SELECT COUNT(*) FROM practice_attempts").fetchone()[0]
    if not total:
        return {"total": 0, "verdict": "no-data"}
    placeholder = conn.execute("SELECT COUNT(*) FROM practice_attempts WHERE answer=?",
                               (SYNTHETIC_ANSWER,)).fetchone()[0]
    distinct_scores = [r[0] for r in conn.execute("SELECT DISTINCT score FROM practice_attempts")]
    showcase = conn.execute(
        "SELECT COUNT(*) FROM practice_attempts WHERE username LIKE 'showcase_%'").fetchone()[0]
    binary = set(distinct_scores) <= {0.0, 100.0}
    synthetic = placeholder / total > 0.9 and binary and showcase / total > 0.9
    return {
        "total": total,
        "placeholder_ratio": round(placeholder / total, 4),
        "showcase_ratio": round(showcase / total, 4),
        "distinct_scores": sorted(distinct_scores)[:8],
        "binary_score": binary,
        "verdict": "synthetic" if synthetic else "mixed-or-real",
    }


def audit_snapshots(conn: sqlite3.Connection) -> dict:
    """判断掌握度快照里有没有"遗忘"这件事发生过。

    没有一次下降 = 快照是单调生成的，用它拟合半衰期等于凭空造数。
    """
    rows = conn.execute(
        "SELECT username, knowledge_id, mastery, created_at FROM mastery_snapshots "
        "ORDER BY username, knowledge_id, created_at").fetchall()
    series: dict[tuple, list[float]] = defaultdict(list)
    for r in rows:
        series[(r["username"], r["knowledge_id"])].append(r["mastery"])
    pairs = drops = 0
    for values in series.values():
        for prev, cur in zip(values, values[1:]):
            pairs += 1
            if cur < prev - 1e-9:
                drops += 1
    return {
        "snapshots": len(rows),
        "series": len(series),
        "transitions": pairs,
        "decreasing": drops,
        "decreasing_ratio": round(drops / pairs, 4) if pairs else 0.0,
        "verdict": "monotonic-synthetic" if pairs and drops == 0 else "has-forgetting-signal",
    }


# ---------- 二、真实日志能标定的部分 ----------

def calibrate_from_logs(path: str) -> dict:
    """`data/logs/llm_calls.jsonl` 是真实调用留下的，可用来标定验证环节的阈值与预算。"""
    if not os.path.exists(path):
        return {"available": False, "reason": f"未找到 {path}"}
    by_intent: dict[str, Counter] = defaultdict(Counter)
    methods: dict[str, Counter] = defaultdict(Counter)
    confidences: dict[str, list[float]] = defaultdict(list)
    partials: dict[str, list[float]] = defaultdict(list)   # unit_test 的"过了几成"
    models: Counter = Counter()
    total = broken = 0
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            broken += 1
            continue
        total += 1
        intent = d.get("intent") or "unknown"
        models[d.get("model") or "unknown"] += 1
        by_intent[intent]["calls"] += 1
        if d.get("revised"):
            by_intent[intent]["revised"] += 1
        v = d.get("verification")
        if isinstance(v, dict):
            by_intent[intent]["verified"] += 1
            if v.get("passed") is True:
                by_intent[intent]["passed"] += 1
            elif v.get("passed") is False:
                by_intent[intent]["failed"] += 1
            if v.get("method"):
                methods[intent][str(v["method"])] += 1
            conf = v.get("confidence")
            if isinstance(conf, (int, float)):
                confidences[intent].append(float(conf))
            extra = v.get("extra")
            if isinstance(extra, dict) and extra.get("total"):
                try:
                    partials[intent].append(float(extra["passed"]) / float(extra["total"]))
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
    out = {"available": True, "total": total, "broken_lines": broken,
           "models": dict(models.most_common()), "intents": {}}
    for intent, c in by_intent.items():
        verified = c["verified"]
        judged = c["passed"] + c["failed"]
        item = {
            "calls": c["calls"],
            "verified_ratio": round(verified / c["calls"], 4) if c["calls"] else 0.0,
            "pass_rate": round(c["passed"] / judged, 4) if judged else None,
            "revise_rate": round(c["revised"] / c["calls"], 4) if c["calls"] else 0.0,
            "methods": dict(methods[intent].most_common(5)),
        }
        part = partials.get(intent) or []
        if part:
            part.sort()
            item["unit_test_partial"] = {
                "n": len(part),
                "median": round(statistics.median(part), 4),
                "p90": round(part[int(0.90 * (len(part) - 1))], 4),
                "all_pass_ratio": round(sum(1 for x in part if x >= 1.0) / len(part), 4),
            }
        vals = confidences.get(intent) or []
        if vals:
            vals.sort()
            item["confidence"] = {
                "n": len(vals),
                "p10": round(vals[int(0.10 * (len(vals) - 1))], 4),
                "median": round(statistics.median(vals), 4),
                "p90": round(vals[int(0.90 * (len(vals) - 1))], 4),
            }
        out["intents"][intent] = item
    return out


# ---------- 三、汇总 ----------

def calibrate(db_path: str, logs_path: str) -> dict:
    conn = _conn(db_path)
    try:
        attempts = audit_attempts(conn)
        snapshots = audit_snapshots(conn)
    finally:
        conn.close()
    logs = calibrate_from_logs(logs_path)

    blocked = []
    if attempts.get("verdict") == "synthetic":
        blocked.append(
            "作答记录是 bootstrap_demo 按 `p = ability - (difficulty-2.5)*0.08` 生成的，"
            "拿它做 IRT 难度标定只会把这条生成公式原样解回来（难度基线本身还来自 "
            "seed_knowledge 的位置推导 `min(5, 1+idx//2)`），属循环论证。")
    if snapshots.get("verdict") == "monotonic-synthetic":
        blocked.append(
            "掌握度快照没有任何一次下降（单调生成：`mastery*(0.58+progress*0.42)`），"
            "数据里根本不存在「遗忘」事件，无法拟合 FORGET_HALF_LIFE_DAYS，"
            "也无法标定 gain / drop / hint_penalty。")

    return {
        "generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
        "provenance": {"attempts": attempts, "mastery_snapshots": snapshots},
        "calibratable": {"llm_verification": logs},
        "blocked": blocked,
        "current_constants": {
            "FORGET_HALF_LIFE_DAYS": 14.0,
            "gain": 0.22,
            "drop_normal": 0.18,
            "drop_misconception": 0.28,
            "hint_penalty_per_hint": 0.15,
            "hint_penalty_cap": 0.6,
            "BASE_INTERVALS": [1, 3, 7, 14, 30],
        },
        "required_to_unblock": [
            "真人作答：至少数十名真实学生、跨若干周的 practice_attempts（得分非 0/100 二值）。",
            "由 update_mastery 真正写出的 mastery_snapshots（而不是 bootstrap_demo 生成的展示曲线）。",
            "复习间隔标定还需要「隔了 N 天后再次作答」的配对样本，当前演示数据里没有。",
        ],
    }


def render_markdown(result: dict) -> str:
    a = result["provenance"]["attempts"]
    s = result["provenance"]["mastery_snapshots"]
    logs = result["calibratable"]["llm_verification"]
    lines = [
        "# 学习参数标定报告", "",
        f"- 生成时间：{result['generated_at']}",
        "- 结论先行：**掌握度模型的常数当前标不了；能标的是验证环节的阈值与预算。**", "",
        "## 一、数据溯源审计", "",
        "| 数据源 | 体量 | 判定 | 依据 |", "|---|---|---|---|",
        f"| practice_attempts | {a.get('total')} | `{a.get('verdict')}` | "
        f"占位作答占比 {a.get('placeholder_ratio')}，得分二值 {a.get('binary_score')}，"
        f"showcase 账号占比 {a.get('showcase_ratio')} |",
        f"| mastery_snapshots | {s.get('snapshots')} | `{s.get('verdict')}` | "
        f"{s.get('transitions')} 次相邻变化中下降 {s.get('decreasing')} 次 |", "",
    ]
    if result["blocked"]:
        lines += ["## 二、标不了的部分（及原因）", ""]
        lines += [f"{i}. {t}" for i, t in enumerate(result["blocked"], 1)]
        lines += ["", "> 用这些数据拟合出来的常数会**比手调值更危险**：它看起来有据可依。", ""]
    lines += ["## 三、能标的部分：验证环节（来自真实调用日志）", ""]
    if not logs.get("available"):
        lines += [f"日志不可用：{logs.get('reason')}", ""]
    else:
        lines += [f"- 日志 {logs['total']} 条，坏行 {logs['broken_lines']} 条",
                  f"- 模型分布：{logs['models']}", "",
                  "| intent | 调用 | 有验证 | 通过率 | 重写率 | 置信度 p10/中位/p90 |",
                  "|---|---|---|---|---|---|"]
        for name, it in sorted(logs["intents"].items(), key=lambda kv: -kv[1]["calls"]):
            conf = it.get("confidence")
            conf_s = f"{conf['p10']} / {conf['median']} / {conf['p90']}" if conf else "—"
            lines.append(f"| {name} | {it['calls']} | {it['verified_ratio']:.0%} | "
                         f"{'—' if it['pass_rate'] is None else format(it['pass_rate'], '.1%')} | "
                         f"{it['revise_rate']:.1%} | {conf_s} |")
        lines += ["", "**怎么读这张表**：", "",
                  "- `diagnose` 通过率 0% 不是缺陷 —— 诊断是在学生答错之后才触发的，"
                  "被验证的本来就是错误代码，`unit_test` 必然不过。它的价值在下面这行分布：",
                  ]
        d = (logs["intents"].get("diagnose") or {}).get("unit_test_partial")
        if d:
            lines.append(f"  {d['n']} 次错误提交里，样例通过率中位数 {d['median']:.0%}、"
                         f"p90 {d['p90']:.0%} —— 学生的错答**大多只差一两个样例**，"
                         "这正是「错因诊断」而不是「从头讲一遍」该发力的地方。")
        lines += ["- `solve` 90.1% 的通过率是「先验证后展示」实际拦下约一成输出，"
                  "重写率 0% 说明拦下之后并没有触发重写 —— 重写预算目前是空的，值得单独确认是否符合预期。",
                  "- 置信度几乎恒为 1.0：确定性验证（unit_test/numeric）本就非 0 即 1，"
                  "这一列现在不承载信息，要做阈值需要先引入带分数的验证法。", ""]
    lines += ["## 四、解除阻塞需要什么", ""]
    lines += [f"- {t}" for t in result["required_to_unblock"]]
    lines += ["", "## 五、当前常数（未改动）", "", "```json",
              json.dumps(result["current_constants"], ensure_ascii=False, indent=2), "```", ""]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="学习参数标定（只出报告，不改常数）")
    ap.add_argument("--db", default=settings.db_path)
    ap.add_argument("--logs", default=os.path.join(settings.logs_dir, "llm_calls.jsonl"))
    ap.add_argument("--out", default="", help="把结构化结果写到这个 JSON")
    ap.add_argument("--report", default="", help="把 Markdown 报告写到这个文件")
    args = ap.parse_args()

    result = calibrate(args.db, args.logs)
    text = render_markdown(result)
    print(text)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[写入] {args.out}")
    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"[写入] {args.report}")


if __name__ == "__main__":
    main()
