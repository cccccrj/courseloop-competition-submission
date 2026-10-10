"""E3 · 题库种子：把已验证的 practice SFT 数据灌入 problems 题库。

practice 每条 assistant 是「先验证后展示」通过的题目 JSON，天然可当固定题库：
- 非代码门：final_answer_canonical → verification_type=numeric（canonical 存 problems.answer）；
            或 rubric → verification_type=rule（rubric_items）。
- 代码门(ds)：code/func_name/test_cases → verification_type=unit_test（沙箱）。
复用 db.insert_problem / add_test_case / add_rubric_item / link_problem_knowledge。

数据源优先级：`data/sft/<course>/practice.clean.jsonl`（三层清洗后的），
没有才退回未清洗的 `practice.jsonl`；用 --source 可强制。

用法：
    python -m scripts.practice_to_bank --all --dry-run     # 只出报告，不写库
    python -m scripts.practice_to_bank --all               # 四门全量灌（--limit 0 = 不限）
    python -m scripts.practice_to_bank --course co --limit 200

重灌语义：每门开灌前只删本来源（source="seed-practice"）的旧题，**不动**真题
（past-paper）与 grow_bank 补的题 —— 原来那句 clear_problem_bank(course) 会把它们一起清掉，
是这个脚本"只能跑一次"的由来。现在可以放心重跑。仍建议执行前 `python -m scripts.backup_userdb`。

知识点归属：不再用自由文本无约束新建，一律先对 courses.py 的大纲做匹配（精确 → 包含 →
字符二元组 Jaccard），匹配不上的挂到「<章节>·其他」兜底节点，再不行挂「未归类」。
"""
from __future__ import annotations

import argparse
import json
import os

from backend.core import db
from backend.core.courses import course_ids, is_valid_course
from scripts.knowledge_align import KnowledgeAligner, stem_key

SFT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "sft"))
CLEAN_NAME = "practice.clean.jsonl"
RAW_NAME = "practice.jsonl"
UNCLASSIFIED = "未归类"

def practice_path(course: str, source: str = "auto") -> str:
    """选数据源：auto 优先 clean，clean/raw 强制。返回空串表示没有可用文件。"""
    clean = os.path.join(SFT_DIR, course, CLEAN_NAME)
    raw = os.path.join(SFT_DIR, course, RAW_NAME)
    if source == "clean":
        return clean if os.path.exists(clean) else ""
    if source == "raw":
        return raw if os.path.exists(raw) else ""
    if os.path.exists(clean):
        return clean
    return raw if os.path.exists(raw) else ""


def _load_practice(course: str, source: str = "auto") -> tuple[list[dict], str, dict]:
    """读题目 JSON 列表，顺带按题干去重。返回 (items, 用的文件, 计数)。"""
    path = practice_path(course, source)
    meta = {"lines": 0, "parsed": 0, "duplicate": 0, "broken": 0}
    if not path:
        return [], "", meta
    out, seen = [], set()
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        meta["lines"] += 1
        try:
            row = json.loads(line)
            obj = json.loads(row["messages"][-1]["content"])
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            meta["broken"] += 1
            continue
        if not isinstance(obj, dict):
            meta["broken"] += 1
            continue
        key = stem_key(obj.get("stem") or "")
        if not key or key in seen:
            meta["duplicate"] += 1
            continue
        seen.add(key)
        meta["parsed"] += 1
        out.append(obj)
    return out, path, meta


def seed_course(course: str, limit: int = 0, source: str = "auto", dry_run: bool = False) -> dict:
    stat = {"numeric": 0, "rule": 0, "unit_test": 0, "skipped": 0}
    items, path, meta = _load_practice(course, source)
    stat["source_file"] = os.path.basename(path) if path else ""
    stat.update({f"src_{k}": v for k, v in meta.items()})
    if limit and limit > 0:
        items = items[:limit]
    aligner = KnowledgeAligner(course)
    if not items:
        print(f"  [{course}] 无 practice 数据可灌（source={source}）")
        stat["align"] = aligner.stats
        return stat
    if dry_run:
        # 只跑对齐统计，不碰数据库
        for obj in items:
            kps = obj.get("knowledge_points") or []
            if kps:
                aligner.align(kps[0])
            code = (obj.get("code") or "").strip()
            if code and (obj.get("func_name") or "").strip() and (obj.get("test_cases") or []):
                stat["unit_test"] += 1
            elif obj.get("final_answer_canonical"):
                stat["numeric"] += 1
            elif obj.get("rubric"):
                stat["rule"] += 1
            else:
                stat["skipped"] += 1
        stat["align"] = aligner.stats
        stat["unmatched"] = aligner.unmatched
        return stat

    removed = db.clear_problem_bank(course, source="seed-practice")
    stat["cleared"] = removed
    for obj in items:
        stem = (obj.get("stem") or "").strip()
        if not stem:
            stat["skipped"] += 1
            continue
        kps = obj.get("knowledge_points") or []
        raw_knowledge = kps[0] if kps else ""
        knowledge, chapter, kdiff = aligner.align(raw_knowledge) if raw_knowledge else ("", "", 2)
        difficulty = int(obj.get("difficulty", 2) or 2)
        ptype = obj.get("type", "练习题")
        canonical = obj.get("final_answer_canonical")
        rubric = obj.get("rubric") or []
        code = (obj.get("code") or "").strip()
        func = (obj.get("func_name") or "").strip()
        tests = obj.get("test_cases") or []
        # 合成数据里本来就带一级提示，一并灌进题库，练习页才有提示可点
        hint = str(obj.get("hint") or "").strip()

        if code and func and tests:               # 代码门
            pid = db.insert_problem(course, ptype, stem, difficulty, obj.get("answer", ""),
                                    "unit_test", knowledge=knowledge, func_name=func,
                                    ref_code=code, source="seed-practice", hint=hint)
            for t in tests:
                if "args" in t and "expected" in t:
                    db.add_test_case(pid, json.dumps(t["args"]), json.dumps(t["expected"]),
                                     1 if t.get("hidden") else 0)
            stat["unit_test"] += 1
        elif canonical:                            # 数值题
            pid = db.insert_problem(course, ptype, stem, difficulty,
                                    json.dumps(canonical, ensure_ascii=False), "numeric",
                                    knowledge=knowledge, source="seed-practice", hint=hint)
            stat["numeric"] += 1
        elif rubric:                               # 概念题（得分点）
            pid = db.insert_problem(course, ptype, stem, difficulty, obj.get("answer", ""),
                                    "rule", knowledge=knowledge, source="seed-practice", hint=hint)
            for r in rubric:
                # evidence 在合成数据里可能是列表（"必须命中的关键词"给了好几个），
                # 直接绑进 sqlite 会 ProgrammingError；统一拍平成字符串。
                ev = r.get("evidence", "")
                if isinstance(ev, (list, tuple)):
                    ev = "；".join(str(x) for x in ev)
                db.add_rubric_item(pid, str(r.get("desc", "")), float(r.get("score", 0) or 0), str(ev))
            stat["rule"] += 1
        else:
            stat["skipped"] += 1
            continue
        if knowledge:
            # 兜底节点也要带 chapter 落库，否则又是一批没有章节归属的空壳
            kid = db.upsert_knowledge_point(course, chapter, knowledge, difficulty=kdiff)
            db.link_problem_knowledge(pid, kid)
    stat["align"] = aligner.stats
    stat["unmatched"] = aligner.unmatched
    return stat


def main():
    ap = argparse.ArgumentParser(description="把 practice SFT 数据灌入固定题库（E3）")
    ap.add_argument("--course", default="", help=f"课程：{course_ids()}")
    ap.add_argument("--all", action="store_true", help="四门都灌")
    ap.add_argument("--limit", type=int, default=0, help="每门最多灌多少题；0=不限（默认）")
    ap.add_argument("--source", choices=["auto", "clean", "raw"], default="auto",
                    help="数据源：auto=优先 practice.clean.jsonl")
    ap.add_argument("--dry-run", action="store_true", help="只出报告，不写库（不会 clear_problem_bank）")
    ap.add_argument("--report", default="", help="把统计写到这个 JSON 文件")
    args = ap.parse_args()

    if not args.dry_run:
        db.init_db()
    targets = course_ids() if args.all else [args.course]
    report = {}
    for cid in targets:
        if not is_valid_course(cid):
            print(f"[跳过] 未知 course={cid}")
            continue
        stat = seed_course(cid, args.limit, args.source, args.dry_run)
        total = stat["numeric"] + stat["rule"] + stat["unit_test"]
        tag = "[dry-run] " if args.dry_run else ""
        print(f"{tag}[{cid}] {total} 题（{stat['source_file']}：读 {stat.get('src_lines',0)} 行 / "
              f"去重丢 {stat.get('src_duplicate',0)} / 坏行 {stat.get('src_broken',0)}）"
              f" numeric={stat['numeric']} rule={stat['rule']} unit_test={stat['unit_test']} "
              f"skipped={stat['skipped']}")
        print(f"          知识点对齐 {stat['align']}"
              + (f"，兜底 {len(stat.get('unmatched', {}))} 种说法" if stat.get("unmatched") else ""))
        if not args.dry_run:
            print(f"          分布 {db.knowledge_distribution(cid)[:5]}")
        report[cid] = stat
    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"报告已写入 {args.report}")
    print("完成。")


if __name__ == "__main__":
    main()
