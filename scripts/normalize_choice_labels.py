"""一次性修复：把用 ①②③④ 标号的选择题和 对/错 判断题改写成 ABCD，让它们重新可判分。

## 起因

`scripts/fix_choice_canonicals.py` 把 84 道判分口径坏掉的题从模考单选池里降级出去了，
但降级只是**止血**——那些题仍然判不对，只是不再出现在卷面的单选段而已。其中两类
其实是**无损可修**的，修完就是货真价实的单选题：

| 类 | 道数 | 现状 | 修法 |
|---|---|---|---|
| ①②③④ 标号的真题 | 6 | 题干里四个选项齐全，只是没用 ABCD | 行首标号 ①→A. ②→B. ③→C. ④→D.，答案同步 |
| 对/错 判断题 | 44 | 题干没有选项，答案是"对"或"错" | 题干补 `A. 正确 / B. 错误`，答案 对→A 错→B |

不修的代价见 `fix_choice_canonicals` 的模块说明：标答是 '②' 或 '对' 时，
`numeric_checker._check_choice` 只抽 A-Z 字母比对，学生填字母**必然判错**、
填原文则抽不到字母退化成 LLM 兜底。**两种都得不到分。**

## 为什么不是"补 rubric"

降级之后它们走 `rule`，判分靠 `rule_checker.check_concept` 的关键点覆盖。但
`_contains` 是**纯子串匹配**：给判断题写一条 evidence="对" 的得分点，学生答
"不对"照样命中——判断题的答案空间只有两个值，子串匹配在这里是错的工具。
改成 ABCD 之后走确定性的字母比对，才是对的工具。

## 判据（两类都必须无损，宁可跳过也不猜）

- **①②③④ 类**：题干里 ①②③④ 四个标号必须**各在行首恰好出现一次**，
  且标答的符号在其中。少一个、多一个、或出现在句中（可能是正文里的枚举而不是选项）
  一律跳过——改坏一道真题比留着它更糟。
- **对/错 类**：标答必须恰好是"对"或"错"，且题干里**没有**已存在的 ABCD 选项行
  （有的话说明它本来就是选择题，另有问题，不该由本脚本处理）。
  题干末尾的 `(√表示正确,×表示错误)` 这类作答说明会被换成真正的选项行。

## 用法

    python -m scripts.normalize_choice_labels                 # 默认 dry-run，只打印
    python -m scripts.normalize_choice_labels --course co     # 只看一门
    python -m scripts.normalize_choice_labels --apply         # 真改（先自动备份）

幂等：改完之后 `verification_type` 回到 numeric、标答是字母，判据查不出行，再跑是 no-op。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import settings  # noqa: E402
from backend.core.courses import course_ids  # noqa: E402

CIRCLED = "①②③④"
_LETTERS = "ABCD"
# 作答说明，不是选项。改写时连同前面的空白一起去掉，免得选项行前面挂一句
# "(√表示正确,×表示错误)" —— 那句话在有了 A/B 两行之后就是多余且矛盾的。
_TF_HINT = re.compile(r"\s*[(（]\s*√\s*表示正确\s*[,，]\s*×\s*表示错误\s*[)）]\s*$")
# 题干里已有的 ABCD 选项行。判断题不该有，有就说明它另有来历，交给人看。
_HAS_LETTER_OPTIONS = re.compile(r"(?m)^\s*[A-D][.、．)]")


def _conn() -> sqlite3.Connection:
    path = Path(settings.db_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _run_isolated(fn, name: str):
    saved, sys.argv = sys.argv, [name]
    try:
        return fn()
    finally:
        sys.argv = saved


def convert_circled(stem: str, value: str) -> tuple[str, str] | None:
    """①②③④ → ABCD。不满足判据返回 None（调用方跳过，不猜）。"""
    value = value.strip()
    if value not in CIRCLED:
        return None
    for ch in CIRCLED:
        # 两个条件都要：全文只出现一次，且那一次在行首。
        # 只查行首会漏掉"下列说法中 ①②③④ 哪个正确。\n① 甲…"这种 —— 改完题干里
        # 会同时留着 ①②③④ 和 A/B/C/D 两套标号；只查总数会把正文里的枚举当选项。
        if stem.count(ch) != 1 or not re.search(rf"(?m)^\s*{ch}", stem):
            return None
    out = stem
    for ch, letter in zip(CIRCLED, _LETTERS):
        out = re.sub(rf"(?m)^(\s*){ch}\s*", rf"\g<1>{letter}. ", out)
    return out, _LETTERS[CIRCLED.index(value)]


def convert_true_false(stem: str, value: str) -> tuple[str, str] | None:
    """对/错 → A. 正确 / B. 错误。不满足判据返回 None。"""
    value = value.strip()
    if value not in ("对", "错"):
        return None
    if _HAS_LETTER_OPTIONS.search(stem):
        return None
    body = _TF_HINT.sub("", stem).rstrip()
    return f"{body}\nA. 正确\nB. 错误", ("A" if value == "对" else "B")


def find_fixable(conn: sqlite3.Connection, course: str = "") -> list[dict]:
    """找出可无损改写的题。只看 rule 题：numeric 的那些要么已经是字母，要么另有问题。"""
    sql = "SELECT * FROM problems WHERE verification_type='rule'"
    params: tuple = ()
    if course:
        sql += " AND course_id=?"
        params = (course,)
    out = []
    for row in conn.execute(sql, params):
        p = dict(row)
        try:
            canon = json.loads(p["answer"] or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(canon, dict) or canon.get("kind") != "choice":
            continue
        value = canon.get("value")
        if not isinstance(value, str):
            continue
        for kind, fn in (("circled", convert_circled), ("true_false", convert_true_false)):
            got = fn(p["stem"], value)
            if got:
                p["_kind"], p["_stem"], p["_letter"], p["_old"] = kind, got[0], got[1], value
                out.append(p)
                break
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--course", default="", help="只处理这一门（默认全部）")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只打印计划）")
    ap.add_argument("--skip-backup", action="store_true", help="跳过备份（不建议）")
    args = ap.parse_args()

    if args.course and args.course not in course_ids():
        print(f"✗ 未知课程 {args.course}", file=sys.stderr)
        return 2

    conn = _conn()
    rows = find_fixable(conn, args.course)
    scope = args.course or "全部课程"
    kinds = {"circled": "①②③④ 标号", "true_false": "对/错 判断题"}
    print(f"▶ {scope}：可无损改写为 ABCD 的题 {len(rows)} 道")
    if not rows:
        print("  · 无（已改过，或本来就没有）")
        return 0

    for kind, label in kinds.items():
        items = [p for p in rows if p["_kind"] == kind]
        if not items:
            continue
        print(f"\n  [{label}] {len(items)} 道")
        for p in items[:2]:
            print(f"      #{p['id']} 答案 {p['_old']} → {p['_letter']}")
            print("      " + p["_stem"][:110].replace("\n", " ⏎ ") + "…")
        if len(items) > 2:
            print(f"      …… 其余 {len(items) - 2} 道")

    if not args.apply:
        print("\n未写库；确认范围后加 --apply。")
        return 0

    if not args.skip_backup:
        print("\n▶ 先备份")
        from scripts.backup_userdb import main as backup_main
        if _run_isolated(backup_main, "backup_userdb") != 0:
            print("✗ 备份失败，中止（不在没有退路的情况下改库）", file=sys.stderr)
            return 2

    with conn:
        for p in rows:
            # 三样一起改：题干换标号、canonical 换字母、判分方式回到 numeric。
            # 少改一样都会留下一个自相矛盾的状态 —— 比如只改题干不改 canonical，
            # 学生看到 A/B/C/D，标答还是 '②'，那就从"判不对"变成"必然判错"。
            conn.execute(
                "UPDATE problems SET stem=?, answer=?, verification_type='numeric' WHERE id=?",
                (p["_stem"], json.dumps({"kind": "choice", "value": p["_letter"]},
                                        ensure_ascii=False), p["id"]))
    left = find_fixable(_conn(), args.course)
    print(f"\n完成：改写 {len(rows)} 道并回到 numeric；复查残留 {len(left)} 道。")
    return 0 if not left else 1


if __name__ == "__main__":
    raise SystemExit(main())
