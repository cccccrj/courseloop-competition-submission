"""数据不变量体检。退出码非 0 即有问题，可挂进部署脚本当闸门。

为什么需要它：串课 bug（掌握度/错题被挂到别门的重名知识点上）在库里存活了很久
没人发现，因为它不报错——记录写进去了，只是**在科目视图下被 JOIN 过滤悄悄吃掉**。
这类"静默正确性"问题靠人眼看页面是发现不了的，只能靠断言。

每条不变量都对应一个真实发生过或差点发生的故障，不是凑数：

  1. 幽灵知识点 = 0
     判据同 scripts/fix_knowledge_course.py。非 0 说明又有代码在建知识点时漏传
     course，修复脚本白跑了。
  2. problem_knowledge 与 problems 课程一致
     关联表是"按知识点取题"的权威映射（个性化练习推荐依赖它）。错配会让学生做
     co 的题却算到 os 的掌握度上。
  3. 有题号的错题都必须有知识点
     problem_id>0 意味着能从题目反查知识点，此时 knowledge_id=0 是纯粹的写入 bug。
     （problem_id=0 的自由问答错题允许无知识点，那是当时就没有。）
  4. 科目视图不吃记录
     每门 list_mastery/list_error_records 加起来，应等于库里有知识点归属的总数。
     少了就说明有记录挂在了不属于任何课程的知识点上——正是串课 bug 的症状。
  5. 作答/错题不指向已删除的题
  6. 作答/错题的知识点与题目课程一致
     这两条防的是**重建题库后的静默错位**。`db.clear_problem_bank` 删该门全部题目
     并 `DELETE FROM sqlite_sequence`，新题 id 从 1 重新开始；而
     practice_attempts.problem_id / error_records.problem_id **没有外键**，
     清库不会连带删作答。于是"先有作答、后重建题库"会让旧作答不是变成孤儿，
     而是**改指向另一道题**——某人"快速排序"的错题挂到某道计组题上，
     页面照常渲染，前四条不变量也全部通过。
     ⑤ 抓题被删光的那部分，零误报；⑥ 抓换了课程的那部分。
     同门内部的错位这两条都抓不到，但整库重建时 id 会跨四门重排，必然触发 ⑥。

用法：
    python -m scripts.check_data
    python -m scripts.check_data --quiet     # 只在失败时输出
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core import db  # noqa: E402
from backend.core.config import settings  # noqa: E402
from backend.core.courses import course_ids  # noqa: E402
from scripts.fix_knowledge_course import PHANTOM_SQL  # noqa: E402


def _conn() -> sqlite3.Connection:
    path = Path(settings.db_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def main() -> int:
    ap = argparse.ArgumentParser(description="数据不变量体检")
    ap.add_argument("--quiet", action="store_true", help="只在失败时输出")
    args = ap.parse_args()

    conn = _conn()
    failures: list[str] = []
    lines: list[str] = []

    def check(name: str, actual, expected, detail: str = ""):
        ok = actual == expected
        lines.append(f"  {'✓' if ok else '✗'} {name}：{actual}"
                     f"{'' if ok else f'（应为 {expected}）'}{f'  {detail}' if detail and not ok else ''}")
        if not ok:
            failures.append(f"{name}: {actual} != {expected}")

    # ① 幽灵知识点
    ghosts = conn.execute(PHANTOM_SQL).fetchall()
    check("幽灵知识点", len(ghosts), 0,
          "跑 python -m scripts.fix_knowledge_course --apply")
    for g in ghosts:
        lines.append(f"      #{g['phantom']} [{g['phantom_course']}] {g['name']} → #{g['canon']}")

    # ② 关联表课程一致性
    mismatch = conn.execute(
        "SELECT COUNT(*) FROM problem_knowledge l "
        "JOIN problems p ON p.id = l.problem_id "
        "JOIN knowledge_points k ON k.id = l.knowledge_id "
        "WHERE p.course_id <> k.course_id").fetchone()[0]
    check("problem_knowledge 课程错配", mismatch, 0)

    # ③ 有题号却无知识点的错题
    orphan = conn.execute(
        "SELECT COUNT(*) FROM error_records "
        "WHERE COALESCE(knowledge_id,0)=0 AND COALESCE(problem_id,0)>0").fetchone()[0]
    check("有题号却无知识点的错题", orphan, 0)

    # ⑤ 指向已删除题目的作答与错题
    for table, label in (("practice_attempts", "作答"), ("error_records", "错题")):
        dangling = conn.execute(
            f"SELECT COUNT(*) FROM {table} t WHERE COALESCE(t.problem_id,0)>0 "
            "AND NOT EXISTS (SELECT 1 FROM problems p WHERE p.id=t.problem_id)").fetchone()[0]
        check(f"指向已删除题目的{label}", dangling, 0,
              "题库被重建过而作答没跟着清；见本文件顶部第 5 条")

    # ⑥ 作答/错题的知识点与题目课程一致
    # 只比课程、不比 problem_knowledge 是否精确命中：作答的 knowledge_id 可能来自
    # 题目的次要知识点，或 TutorGraph 那条按会话科目现建的路径（tutor_graph 调
    # get_or_create_knowledge_id），要求精确命中会大量误报。跨课程则一定是错的。
    for table, label in (("practice_attempts", "作答"), ("error_records", "错题")):
        crossed = conn.execute(
            f"SELECT COUNT(*) FROM {table} t "
            "JOIN problems p ON p.id = t.problem_id "
            "JOIN knowledge_points k ON k.id = t.knowledge_id "
            "WHERE p.course_id <> k.course_id").fetchone()[0]
        check(f"{label}的知识点与题目课程错配", crossed, 0,
              "题库重建导致 problem_id 错位；见本文件顶部第 6 条")

    # ④ 科目视图不吃记录：分课程加总 == 库里有知识点归属的总数
    courses = list(course_ids())
    users = [r["username"] for r in conn.execute(
        "SELECT DISTINCT username FROM mastery_records "
        "UNION SELECT DISTINCT username FROM error_records")]
    for u in sorted(users):
        # 分母只算"知识点确实存在"的记录：knowledge_id=0 或指向已删知识点的，
        # 本来就不该出现在任何科目视图里，不算视图吃了记录。
        m_total = conn.execute(
            "SELECT COUNT(*) FROM mastery_records m JOIN knowledge_points k ON k.id=m.knowledge_id "
            "WHERE m.username=?", (u,)).fetchone()[0]
        e_total = conn.execute(
            "SELECT COUNT(*) FROM error_records e JOIN knowledge_points k ON k.id=e.knowledge_id "
            "WHERE e.username=?", (u,)).fetchone()[0]
        m_seen = sum(len(db.list_mastery(u, course=c)) for c in courses)
        e_seen = sum(len(db.list_error_records(u, limit=9999, course=c)) for c in courses)
        check(f"{u} 掌握度科目视图可见数", m_seen, m_total)
        check(f"{u} 错题科目视图可见数", e_seen, e_total)

    conn.close()

    if failures and args.quiet:
        print("✗ 数据体检未通过：")
    if not args.quiet or failures:
        print("▶ 数据不变量体检")
        print("\n".join(lines))
        print(f"\n{'✗ ' + str(len(failures)) + ' 项未通过' if failures else '✓ 全部通过'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
