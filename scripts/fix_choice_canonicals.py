"""一次性修复：把判分口径已经坏掉的"假选择题"从模考单选池里降级出去。

## 起因

`mock_exam.is_choice()` 的判据是 `verification_type='numeric'` 且 canonical 的
`kind=='choice'`——它**不看 value 长什么样**。于是库里出现这样的题：

    {"kind": "choice", "value": {"yiaddr": "192.168.1.100", "src_port": 67}}
    {"kind": "choice", "value": "不能"}
    {"kind": "choice", "value": "②"}

前两条本来是计算题/简答题/判断题，被出题或切题模型顺手标成了 choice；第三条是真题，
选项用 ①②③④ 而不是 ABCD 标号。三种都会被抽进模考的**单选段**，于是卷面上出现
一道没有 ABCD 可选的"单选题"，而判分器 `numeric_checker._check_choice`
只从作答里抽 A-Z 字母（`_letters`）去比对：

    标答 '对'              学生填 B  → passed=False   确定性判错
    标答 '②'               学生填 B  → passed=False   确定性判错
    标答 {'src_port': 67}  学生填 B  → passed=None    校验抛异常，退化 LLM 兜底

**主要后果是判错，不是判不了。** 84 道里 67 道属前者：学生填任何字母都是红叉，
而成绩单上看不出任何异常；其余 23 道结构化答案掉进 `judge_answer` 的
`numeric+llm` 兜底，由 LLM 拿一个字典当"标准答案"去判。

`scripts/ingest_past_papers.canonical_ok()` 已经在入库口把这类挡住了（不合格的
降级成 rule，题干答案都留着，只是不进选择题池），但它只管**新灌的**。本脚本管
已经在库里的——那批题早于那道闸，或者根本不走 `ingest_past_papers`
（`seed-practice` 那 28 道就是）。

## 判据

复用 `ingest_past_papers.canonical_ok()`，不另写一套。这里再写一份"什么算合格的
选择题答案"的话，两处迟早分叉，症状是入库口放过的题被本脚本降级、或者反过来。

## 处理方式

只改 `verification_type`: `numeric` → `rule`。**不动 answer、不删题**：
题干和答案都还在，练习/讲评照常出，判分改走规则与讲解那一路；变的只是
"它不再被当成模考单选题"。降级是可逆的——把 canonical 修对再改回 numeric 即可。

## 用法

    python -m scripts.fix_choice_canonicals                 # 默认 dry-run，只打印
    python -m scripts.fix_choice_canonicals --course co     # 只看一门
    python -m scripts.fix_choice_canonicals --apply         # 真改（先自动备份）

幂等：跑完一次后判据查不出行，再跑就是全 no-op。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import settings  # noqa: E402
from backend.core.courses import course_ids  # noqa: E402
from backend.learning.mock_exam import is_choice  # noqa: E402
from scripts.ingest_past_papers import canonical_ok  # noqa: E402


def _conn() -> sqlite3.Connection:
    path = Path(settings.db_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _run_isolated(fn, name: str):
    """调另一个脚本的 main()。那些 main() 自己 argparse sys.argv，直接调会吃到本脚本的参数。"""
    saved, sys.argv = sys.argv, [name]
    try:
        return fn()
    finally:
        sys.argv = saved


def find_broken(conn: sqlite3.Connection, course: str = "") -> list[dict]:
    """挑出"在模考单选池里、但 canonical 判不了分"的题。

    先过 `is_choice` 再过 `canonical_ok`：只有同时"组卷认它是单选"和"判分器认不了它"
    的题才是坏的。反过来（canonical 不合格但组卷也不要它）不构成问题，不要动它——
    改一条与症状无关的记录，只会让这次迁移的账对不上。
    """
    sql = "SELECT * FROM problems"
    params: tuple = ()
    if course:
        sql += " WHERE course_id=?"
        params = (course,)
    out = []
    for row in conn.execute(sql, params):
        p = dict(row)
        if not is_choice(p):
            continue
        try:
            canon = json.loads(p["answer"] or "")
        except (json.JSONDecodeError, TypeError):
            canon = None
        if canonical_ok(canon):
            continue
        p["_value"] = str((canon or {}).get("value"))
        out.append(p)
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
    broken = find_broken(conn, args.course)
    scope = args.course or "全部课程"
    print(f"▶ {scope}：模考单选池里判分口径坏掉的题 {len(broken)} 道")
    if not broken:
        print("  · 无（已修复过，或本来就干净）")
        return 0

    by_course: dict[str, list[dict]] = {}
    for p in broken:
        by_course.setdefault(p["course_id"], []).append(p)
    for cid, items in sorted(by_course.items()):
        srcs: dict[str, int] = {}
        for p in items:
            srcs[p["source"] or "?"] = srcs.get(p["source"] or "?", 0) + 1
        print(f"\n  [{cid}] {len(items)} 道  来源：" +
              "、".join(f"{k} {v}" for k, v in sorted(srcs.items(), key=lambda t: -t[1])))
        for p in items[:3]:
            print(f"      #{p['id']} value={p['_value'][:48]!r}  {p['stem'][:44]}…")
        if len(items) > 3:
            print(f"      …… 其余 {len(items) - 3} 道")

    if not args.apply:
        print("\n未写库；确认范围后加 --apply。")
        return 0

    if not args.skip_backup:
        # 放在这里而不是叮嘱人"记得先备份"——要改唯一权威库，靠记性是不行的
        print("\n▶ 先备份")
        from scripts.backup_userdb import main as backup_main
        if _run_isolated(backup_main, "backup_userdb") != 0:
            print("✗ 备份失败，中止（不在没有退路的情况下改库）", file=sys.stderr)
            return 2

    with conn:
        conn.executemany("UPDATE problems SET verification_type='rule' WHERE id=?",
                         [(p["id"],) for p in broken])
    left = find_broken(_conn(), args.course)
    print(f"\n完成：降级 {len(broken)} 道 numeric → rule；复查残留 {len(left)} 道。")
    return 0 if not left else 1


if __name__ == "__main__":
    raise SystemExit(main())
