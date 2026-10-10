"""清掉已入库的模型输出里的控制标记（I-02），并把代码题的原始报错改写成中文（I-05）。

修复之后新回答在入库前就会清洗，但修复前存进去的历史回答还带着
「---- 回答完毕 ----」「证据引用处理完毕」这类行。前端读回时会兜底再洗一遍，
这个脚本把库里的数据也一次性改干净：一是导出、助教复核等不经过前端的路径
也能拿到干净文本；二是历史回答会被回灌给模型，留着标记它会照着学。

I-05 的存量：代码题判错时，沙箱的原始报错（`代码无法加载: SyntaxError: invalid syntax
(<string>, line 1)`）曾被原样存成错题诊断，助教复核卡片和学生记录页都看得到。这里改写成
"代码有语法错误（第 1 行），无法运行"，原文挪进 tech_detail 列（助教可在"技术详情"里展开）。

默认只预览，列出将改动的行和前后对比；加 --apply 才写库。可重复执行，
第二次跑应当显示 0 行。

    python scripts/clean_dialog_markers.py            # 预览
    python scripts/clean_dialog_markers.py --apply    # 写库
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import settings  # noqa: E402
from backend.core.sanitize import clean_answer, strip_markers  # noqa: E402
from backend.verification.code_errors import rewrite_legacy  # noqa: E402

# (表, 列, 清洗函数, 额外 WHERE)。大厅回答是散文，用 clean_answer；
# 诊断和复习摘要可能是结构化文本，只删控制行。
TARGETS = [
    ("dialog_turns", "content", clean_answer, "role = 'assistant'"),
    ("error_records", "diagnosis", strip_markers, "1 = 1"),
    ("review_plans", "summary", strip_markers, "1 = 1"),
]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _short(text: str, n: int = 120) -> str:
    one = " ⏎ ".join((text or "").splitlines())
    return one if len(one) <= n else one[:n] + "…"


def _rewrite_code_errors(conn: sqlite3.Connection, apply: bool) -> int:
    cols = _columns(conn, "error_records")
    if "diagnosis" not in cols:
        return 0
    has_tech = "tech_detail" in cols
    rows = conn.execute(
        "SELECT id, diagnosis" + (", COALESCE(tech_detail,'')" if has_tech else ", ''") +
        " FROM error_records WHERE diagnosis LIKE '%代码无法加载%' OR diagnosis LIKE '%未通过：%Error%'"
        " OR diagnosis LIKE '%signal%' OR diagnosis LIKE '%沙箱%' OR diagnosis LIKE '%未找到函数%'"
    ).fetchall()
    changed = []
    for rid, old, tech in rows:
        new, raw = rewrite_legacy(old)
        if new != old:
            changed.append((rid, old, new, tech or raw))
    print(f"error_records.diagnosis（代码原始报错）: 检查 {len(rows)} 行，需要改写 {len(changed)} 行")
    for rid, old, new, _ in changed:
        print(f"  #{rid}\n    前：{_short(old)}\n    后：{_short(new)}")
    if apply and changed:
        if has_tech:
            conn.executemany("UPDATE error_records SET diagnosis = ?, tech_detail = ? WHERE id = ?",
                             [(new, tech, rid) for rid, _, new, tech in changed])
        else:
            conn.executemany("UPDATE error_records SET diagnosis = ? WHERE id = ?",
                             [(new, rid) for rid, _, new, _ in changed])
    return len(changed)


def run(db_path: str, apply: bool) -> int:
    conn = sqlite3.connect(db_path)
    total = 0
    for table, col, clean, where in TARGETS:
        if col not in _columns(conn, table):
            continue
        rows = conn.execute(
            f"SELECT id, {col} FROM {table} WHERE {where} AND {col} IS NOT NULL AND {col} != ''"
        ).fetchall()
        changed = [(rid, old, clean(old)) for rid, old in rows]
        changed = [(rid, old, new) for rid, old, new in changed if new != old.strip()]
        print(f"{table}.{col}: 检查 {len(rows)} 行，需要清洗 {len(changed)} 行")
        for rid, old, new in changed:
            print(f"  #{rid}\n    前：{_short(old)}\n    后：{_short(new)}")
        if apply and changed:
            conn.executemany(f"UPDATE {table} SET {col} = ? WHERE id = ?",
                             [(new, rid) for rid, _, new in changed])
        total += len(changed)
    total += _rewrite_code_errors(conn, apply)
    if apply:
        conn.commit()
    conn.close()
    print(f"\n共 {total} 行{'已写回' if apply else '待清洗（预览，未改库；加 --apply 写入）'}")
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=settings.db_path, help="SQLite 路径，默认读配置")
    parser.add_argument("--apply", action="store_true", help="写回数据库（默认只预览）")
    args = parser.parse_args()
    run(args.db, args.apply)


if __name__ == "__main__":
    main()
