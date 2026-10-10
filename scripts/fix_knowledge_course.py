"""一次性修复：把挂错课程的"幽灵知识点"及其记录搬回正主。

## 起因

`db.get_or_create_knowledge_id(name, course_id)` 曾有默认值 `"ds-algo"`，而
`/problems/judge` 的调用点没传 course。于是学生做完一道 co/os/net 的题，掌握度与
错题被挂到一个**新建的 ds-algo 版重名知识点**上。

后果不是"数据难看"而是"数据消失"：`list_mastery` / `list_error_records` 都按
`knowledge_points.course_id` 内连接过滤（db.py），科目视图会把这些记录整批吃掉——
学生在错题本里一条都看不到自己刚做错的题。

代码侧已根治（course_id 改为必传，判题优先走 `problem_knowledge` 关联表），
本脚本负责清掉已经产生的脏数据。

## 判据

判一个知识点是"幽灵"要三个条件同时成立，缺一不可：

  1. `anchor = 0`  —— 它自己没有任何题（既无 problem_knowledge 关联，也无同门同名题）
  2. `chapter = ''` —— 不是 seed_knowledge 按大纲播种的正牌点，是运行时建的空壳
  3. 存在同名兄弟且那个兄弟 `anchor > 0` —— 有题锚定，说明它才是正主

第 2、3 条是用来**避免误伤**的：`48 co/虚拟存储器` 与 `152 os/虚拟存储器` 同名，
但两个都合法（48 有大纲 chapter，152 有 4 道 os 题）。只看重名会把它们判成一对
幽灵/正主，把 os 的记录搬到 co 去——那是比原 bug 更糟的破坏。

## 用法

    python -m scripts.fix_knowledge_course              # 默认 dry-run，只打印将要做什么
    python -m scripts.fix_knowledge_course --apply      # 真改（先自动备份）

幂等：跑完一次后判据查不出行，再跑就是全 no-op。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core import db  # noqa: E402
from backend.core.config import settings  # noqa: E402

# 幽灵点判据。anchor = 关联表里的题数 + 同门同名题数；三个条件见模块 docstring。
PHANTOM_SQL = """
WITH a AS (
  SELECT k.id, k.course_id, k.name, k.chapter,
    (SELECT COUNT(*) FROM problem_knowledge pk WHERE pk.knowledge_id = k.id)
  + (SELECT COUNT(*) FROM problems p WHERE p.course_id = k.course_id AND p.knowledge = k.name)
    AS anchor
  FROM knowledge_points k)
SELECT a.id AS phantom, a.course_id AS phantom_course, a.name,
       b.id AS canon, b.course_id AS canon_course, b.anchor AS canon_anchor
FROM a JOIN a b ON b.name = a.name AND b.id <> a.id
WHERE a.anchor = 0
  AND COALESCE(a.chapter, '') = ''
  AND b.anchor > 0
ORDER BY a.id
"""

# 引用了 knowledge_id 的表。problem_knowledge 按幽灵点的定义必然是 0 行，
# 仍然列进来：判据将来若放宽，漏改一张表就是静默的数据错位。
REF_TABLES = ("error_records", "review_tasks", "problem_knowledge")


def _run_isolated(fn, name: str):
    """调用另一个脚本的 main()。

    那些 main() 都自己 argparse `sys.argv`，直接调会把本脚本的 `--apply` 喂给它们
    并当场报"unrecognized arguments"。临时换掉 argv 是最小侵入的隔离方式。
    """
    saved, sys.argv = sys.argv, [name]
    try:
        return fn()
    finally:
        sys.argv = saved


def _conn() -> sqlite3.Connection:
    path = Path(settings.db_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _snapshot(conn: sqlite3.Connection) -> dict:
    """修复前后各拍一次：用可见记录数证明"记录是搬到了正主，不是被删了"。"""
    out = {}
    for r in conn.execute(
            "SELECT DISTINCT username FROM mastery_records "
            "UNION SELECT DISTINCT username FROM error_records"):
        u = r["username"]
        for course in ("ds-algo", "co", "os", "net"):
            out[(u, course)] = (
                len(db.list_error_records(u, limit=999, course=course)),
                len(db.list_mastery(u, course=course)),
            )
    return out


def _migrate_mastery(conn: sqlite3.Connection, phantom: int, canon: int, apply: bool) -> list[str]:
    """搬掌握度。mastery_records 主键是 (username, knowledge_id)，直接 UPDATE 会撞主键。

    不能用 `UPDATE OR IGNORE`：那会静默保留 canon 行、丢掉 phantom 行，还留下一条
    没人再查得到的垃圾。冲突时按 (confidence, last_practiced_at) 择优保留。
    """
    notes = []
    rows = conn.execute(
        "SELECT * FROM mastery_records WHERE knowledge_id=?", (phantom,)).fetchall()
    for row in rows:
        u = row["username"]
        exist = conn.execute(
            "SELECT * FROM mastery_records WHERE username=? AND knowledge_id=?",
            (u, canon)).fetchone()
        if exist is None:
            notes.append(f"      {u}: 掌握度 {row['mastery']} 搬到 #{canon}（正主无记录，直接迁移）")
            if apply:
                conn.execute(
                    "UPDATE mastery_records SET knowledge_id=? WHERE username=? AND knowledge_id=?",
                    (canon, u, phantom))
        else:
            keep = max((exist, row),
                       key=lambda r: ((r["confidence"] or 0), r["last_practiced_at"] or ""))
            src = "幽灵" if keep is row else "正主"
            notes.append(f"      {u}: 两边都有记录，保留{src}的 mastery={keep['mastery']}")
            if apply:
                conn.execute(
                    "UPDATE mastery_records SET mastery=?, confidence=?, state=?, last_practiced_at=? "
                    "WHERE username=? AND knowledge_id=?",
                    (keep["mastery"], keep["confidence"], keep["state"],
                     keep["last_practiced_at"], u, canon))
                conn.execute("DELETE FROM mastery_records WHERE username=? AND knowledge_id=?",
                             (u, phantom))
    return notes


def _backfill_orphan_errors(conn: sqlite3.Connection, apply: bool) -> tuple[int, int]:
    """回填 knowledge_id=0 的历史错题。

    只补 problem_id>0 的（能从题目反查到确定的知识点）。problem_id=0 的来自
    TutorGraph 自由问答，当时 sess.knowledge 就是空的——**保持原样不猜**，
    猜错的归属比缺失更糟：它会污染掌握度排序，而且再也看不出是猜的。
    """
    fixed = 0
    rows = conn.execute(
        "SELECT id, problem_id FROM error_records WHERE COALESCE(knowledge_id,0)=0").fetchall()
    unfixable = 0
    for r in rows:
        if not r["problem_id"]:
            unfixable += 1
            continue
        kid, _ = db.knowledge_id_of_problem(r["problem_id"])
        if not kid:
            unfixable += 1
            continue
        if apply:
            conn.execute("UPDATE error_records SET knowledge_id=? WHERE id=?", (kid, r["id"]))
        fixed += 1
    return fixed, unfixable


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只打印计划）")
    ap.add_argument("--skip-backup", action="store_true", help="跳过备份（不建议）")
    args = ap.parse_args()

    if args.apply and not args.skip_backup:
        # 放在这里而不是叮嘱人"记得先备份"——要改唯一权威库，靠记性是不行的
        print("▶ 先备份")
        from scripts.backup_userdb import main as backup_main
        if _run_isolated(backup_main, "backup_userdb") != 0:
            print("✗ 备份失败，中止（不在没有退路的情况下改库）", file=sys.stderr)
            return 2
        print()

    conn = _conn()
    before = _snapshot(conn)

    phantoms = [dict(r) for r in conn.execute(PHANTOM_SQL)]
    print(f"▶ 幽灵知识点：{len(phantoms)} 个")
    if not phantoms:
        print("  · 无（已修复过，或本来就干净）")

    for p in phantoms:
        ref_counts = {t: conn.execute(
            f"SELECT COUNT(*) FROM {t} WHERE knowledge_id=?", (p["phantom"],)).fetchone()[0]
            for t in REF_TABLES}
        n_mastery = conn.execute(
            "SELECT COUNT(*) FROM mastery_records WHERE knowledge_id=?", (p["phantom"],)).fetchone()[0]
        print(f"\n  #{p['phantom']} [{p['phantom_course']}] {p['name']}"
              f"  →  #{p['canon']} [{p['canon_course']}]（有 {p['canon_anchor']} 道题锚定）")
        for t, n in ref_counts.items():
            if n:
                print(f"      {t}: {n} 条改挂")
        if n_mastery:
            print(f"      mastery_records: {n_mastery} 条")

    if args.apply and phantoms:
        # 整个搬迁走一个事务：中途崩了也不会留下"错题搬了、掌握度没搬"的半吊子状态
        try:
            conn.execute("BEGIN")
            for p in phantoms:
                for t in REF_TABLES:
                    conn.execute(f"UPDATE {t} SET knowledge_id=? WHERE knowledge_id=?",
                                 (p["canon"], p["phantom"]))
                for note in _migrate_mastery(conn, p["phantom"], p["canon"], apply=True):
                    print(note)
                conn.execute("DELETE FROM knowledge_points WHERE id=?", (p["phantom"],))
            conn.commit()
            print("\n  ✓ 搬迁完成")
        except Exception:
            conn.rollback()
            print("\n✗ 搬迁失败，已回滚（库未改动）", file=sys.stderr)
            raise
    elif phantoms:
        for p in phantoms:
            for note in _migrate_mastery(conn, p["phantom"], p["canon"], apply=False):
                print(note)

    print()
    fixed, unfixable = _backfill_orphan_errors(conn, apply=args.apply)
    if args.apply:
        conn.commit()
    print(f"▶ 无知识点的历史错题：回填 {fixed} 条"
          f"{f'，{unfixable} 条无从判定（problem_id=0 的自由问答），按原样保留' if unfixable else ''}")

    if args.apply:
        print("\n▶ 回填章节归属（seed_knowledge，幂等）")
        from scripts.seed_knowledge import main as seed_main
        _run_isolated(seed_main, "seed_knowledge")

    # 自验证：重跑判据应为 0 行，且各科目可见记录数只增不减
    print("\n▶ 自验证")
    left = conn.execute(PHANTOM_SQL).fetchall()
    if args.apply:
        print(f"  幽灵点残留：{len(left)} 个 {'✓' if not left else '✗ 仍有残留'}")
    else:
        print(f"  幽灵点残留：{len(left)} 个（dry-run 未改动，符合预期）")

    after = _snapshot(conn)
    changed = [(k, before[k], after.get(k, (0, 0)))
               for k in before if after.get(k, (0, 0)) != before[k]]
    for (u, course), (e0, m0), (e1, m1) in changed:
        print(f"  {u} / {course}: 错题 {e0}→{e1} · 掌握度 {m0}→{m1}")

    # 判"有没有丢数据"必须看**每个用户跨科目的总数**，不能按科目看：
    # 记录本来就是从挂错的那门搬到正确的那门，源科目变少正是修复生效的样子。
    ok = True
    users = {u for u, _ in before}
    for u in sorted(users):
        t0 = tuple(sum(x) for x in zip(*[before[(u, c)] for c in ("ds-algo", "co", "os", "net")]))
        t1 = tuple(sum(x) for x in zip(*[after[(u, c)] for c in ("ds-algo", "co", "os", "net")]))
        good = t1[0] >= t0[0] and t1[1] >= t0[1]
        ok = ok and good
        print(f"  {u} 合计：错题 {t0[0]}→{t1[0]} · 掌握度 {t0[1]}→{t1[1]}"
              f"  {'✓ 只增不减' if good else '✗ 有记录丢失！'}")
    if not changed:
        print("  各科目可见记录数无变化" + ("（dry-run，符合预期）" if not args.apply else ""))

    conn.close()
    if not args.apply:
        print("\n以上为 dry-run。确认无误后加 --apply 执行。")
        return 0
    return 0 if (not left and ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
