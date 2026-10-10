"""data/raw 课件教材 → SQLite FTS5 检索库（不装 torch / 不建向量库）。

与 ``build_light_rag.py`` 的分工：那个脚本从既有 Chroma 导出；这个直接从原始 PDF/PPTX 解析，
适用于「有语料、没有也建不起向量库」的机器 —— 也就是当前交接环境。

复用而非重写：
- 解析端 = ``backend.rag.ingest.discover_corpus`` + ``parse_one``（纯解析，只要 PyMuPDF/python-pptx）；
- 表结构 = ``scripts.build_light_rag`` 的同一套 FTS5 schema，一列都不能差，
  否则 ``LightRagEngine.retrieve()`` 的 ``bm25(chunks)`` 排序会失效。

用法：
    python -m scripts.build_fts_from_raw --dry-run           # 只列要解析哪些文件
    python -m scripts.build_fts_from_raw                     # 全量重建
    python -m scripts.build_fts_from_raw --resume            # 跳过已入库的文件，接着跑
    python -m scripts.build_fts_from_raw --course co         # 只建某门

建完把 .env 的 CL_RAG_MODE 改成 fts5（CL_RAG_FTS_PATH 缺省即 data/processed/rag_fts.db）。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time
from pathlib import Path

from backend.core.config import settings
from backend.rag import indexer


def _done_sources(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT DISTINCT source FROM chunks").fetchall()}


def build(out_path: Path, course: str = "", resume: bool = False,
          dry_run: bool = False, limit: int = 0) -> dict:
    # 解析端延迟导入：backend.rag.ingest 顶层已无 chromadb/llama_index，但这里仍晚导一次，
    # 让 --dry-run 在缺 PyMuPDF 的机器上也能列清单。
    from backend.rag.ingest import DEFAULT_CORPUS, discover_corpus

    raw_dir = Path(settings.raw_dir)
    corpus = DEFAULT_CORPUS + discover_corpus(raw_dir)
    if course:
        corpus = [it for it in corpus if it.get("course") == course]
    if limit:
        corpus = corpus[:limit]

    if dry_run:
        for it in corpus:
            print(f"  {it['course']:<8} {it['doc_type']:<10} {it['parser']:<10} {it['file']}")
        return {"files": len(corpus), "dry_run": True}

    if not resume and out_path.exists():
        out_path.unlink()
    conn = indexer.connect(out_path)
    backfilled = indexer.backfill_hashes(conn)
    if backfilled:
        print(f"      [去重] 给已有的 {backfilled} 块补了内容哈希")
    done = _done_sources(conn) if resume else set()
    stat = {"files": 0, "skipped": 0, "chunks": 0, "duplicate": 0, "failed": []}
    t0 = time.time()
    try:
        for idx, item in enumerate(corpus, 1):
            if resume and item["name"] in done:
                stat["skipped"] += 1
                continue
            try:
                one = indexer.index_one(conn, item, raw_dir)   # 逐文件提交，中断也不白跑
            except Exception as exc:                     # 单个坏文件不该拖垮整轮
                print(f"[失败] {item['file']}: {type(exc).__name__}: {exc}")
                stat["failed"].append(item["file"])
                continue
            stat["files"] += 1
            stat["chunks"] += one["written"]
            stat["duplicate"] += one["duplicate"]
            dup = f"，跳过重复 {one['duplicate']}" if one["duplicate"] else ""
            print(f"      [{idx}/{len(corpus)}] 累计 {stat['chunks']} 块{dup}，"
                  f"用时 {time.time() - t0:.0f}s")
        indexer.stamp_meta(conn, "build_fts_from_raw", raw_dir)
    finally:
        conn.close()
    stat["seconds"] = round(time.time() - t0, 1)
    stat["output"] = str(out_path)
    stat["bytes"] = out_path.stat().st_size if out_path.exists() else 0
    return stat


def main() -> None:
    ap = argparse.ArgumentParser(description="data/raw → SQLite FTS5 轻量检索库")
    ap.add_argument("--out", type=Path, default=Path(settings.rag_fts_path))
    ap.add_argument("--course", default="", help="只建某门：ds-algo|co|os|net")
    ap.add_argument("--resume", action="store_true", help="跳过已入库的文件（按 source 名）")
    ap.add_argument("--dry-run", action="store_true", help="只列清单不解析")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个文件（调试用）")
    args = ap.parse_args()
    stat = build(args.out, args.course, args.resume, args.dry_run, args.limit)
    print(stat)


if __name__ == "__main__":
    main()
