"""知识点树播种（E1）：把 courses.py 四门 knowledge_taxonomy 写入 knowledge_points 表。

courses.py 的 knowledge_taxonomy（[{chapter, topics[]}]，依 2026 官方 408 大纲校准）此前从未进库，
掌握度/错题按知识点归档时只能靠 get_or_create 建空壳（chapter 空、difficulty 固定 2）。
本脚本按章节顺序播种，带 chapter 归属与按章递增的难度基线，供前端知识树/章节浏览与按章聚合。

用法（rag_env）：
    python -m scripts.seed_knowledge              # 播种全部四门
    python -m scripts.seed_knowledge --course co  # 只播某门
幂等：按 (course_id, name) upsert，重复跑只回填不重建。
"""
from __future__ import annotations

import argparse

from backend.core import db
from backend.core.courses import all_courses, get_course, is_valid_course, course_ids


def seed_course(course_id: str) -> int:
    c = get_course(course_id)
    n = 0
    # 难度基线：按章节递进（越后面的章节默认略难），1~5 封顶
    for idx, ch in enumerate(c.knowledge_taxonomy):
        chapter = ch.get("chapter", "")
        base_diff = min(5, 1 + idx // 2)  # 每两章 +1
        for name in ch.get("topics", []):
            db.upsert_knowledge_point(course_id, chapter, name, difficulty=base_diff)
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description="知识点树播种（四门 408）")
    ap.add_argument("--course", default="", help=f"只播某门：{course_ids()}；缺省播全部")
    args = ap.parse_args()

    db.init_db()
    targets = [args.course] if args.course else list(all_courses())
    for cid in targets:
        if not is_valid_course(cid):
            print(f"[跳过] 未知 course={cid}")
            continue
        n = seed_course(cid)
        pts = db.list_knowledge_points(cid)
        chapters = sorted({p["chapter"] for p in pts})
        print(f"[{cid}] 播种 {n} 个知识点，覆盖 {len(chapters)} 章：{chapters}")
    print("完成。")


if __name__ == "__main__":
    main()
