"""把已有 Chroma 文档导出为低内存 SQLite FTS5 trigram 索引。"""
from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

# 表结构与 build_fts_from_raw 共用同一个常量：两个脚本写的是同一张表，
# 列名/顺序/UNINDEXED 一旦漂移，LightRagEngine 的 bm25(chunks) 排序就会静默失效。
from backend.rag.indexer import FTS_DDL


def build(source: Path, output: Path, collection_name: str = "courseloop") -> dict:
    import chromadb

    client = chromadb.PersistentClient(path=str(source))
    collection = client.get_collection(collection_name)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.unlink(missing_ok=True)
    conn = sqlite3.connect(temporary)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute(FTS_DDL)
        total, offset, page_size = 0, 0, 500
        while True:
            batch = collection.get(limit=page_size, offset=offset,
                                   include=["documents", "metadatas"])
            documents = batch.get("documents") or []
            metadatas = batch.get("metadatas") or []
            if not documents:
                break
            rows = []
            for text, meta in zip(documents, metadatas):
                meta = meta or {}
                rows.append((text or "", meta.get("course", ""), meta.get("doc_type", ""),
                             meta.get("source", "未知来源"), meta.get("title_path", ""),
                             int(meta.get("page_start", 0) or 0), int(meta.get("page_end", 0) or 0),
                             int(meta.get("chunk_index", -1) or -1)))
            conn.executemany("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?)", rows)
            total += len(rows)
            offset += len(documents)
            if len(documents) < page_size:
                break
        conn.execute("CREATE TABLE build_meta(key TEXT PRIMARY KEY,value TEXT)")
        conn.executemany("INSERT INTO build_meta VALUES(?,?)",
                         [("source", str(source)), ("collection", collection_name),
                          ("documents", str(total))])
        conn.commit()
    finally:
        conn.close()
    os.replace(temporary, output)
    return {"documents": total, "output": str(output), "bytes": output.stat().st_size}


def main() -> None:
    parser = argparse.ArgumentParser(description="Chroma → SQLite FTS5 轻量演示索引")
    parser.add_argument("--source-chroma", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--collection", default="courseloop")
    args = parser.parse_args()
    print(build(args.source_chroma, args.out, args.collection))


if __name__ == "__main__":
    main()
