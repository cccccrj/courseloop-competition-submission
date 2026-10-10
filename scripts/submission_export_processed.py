"""Export processed course content into new databases, never production identities.

Runs with the standard library on the source host. Source SQLite connections are
read-only; the destination must be a new directory. Only explicitly listed
content tables and FTS columns are copied. The export contains no environment
files, user rows, learning histories or instance configuration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

CONTENT_TABLES = (
    "courses", "knowledge_points", "problems", "test_cases", "rubric_items",
    "problem_knowledge",
)
FTS_COLUMNS = (
    "text", "course", "doc_type", "source", "title_path", "page_start",
    "page_end", "chunk_index",
)
ATTRIBUTION = "部分数据来源于南京理工大学计算机科学与工程学院本科生自建数据库。"


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def readonly(path):
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    return conn


def export(source: Path, output: Path):
    if output.exists():
        raise ValueError("Destination exists; refusing to overwrite")
    business_path = source / "data/processed/courseloop.db"
    rag_path = source / "data/processed/rag_fts.db"
    # Validate both sources before creating any destination.
    with readonly(business_path) as src, readonly(rag_path) as rag:
        for conn in (src, rag):
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Source database integrity failed")
        schema = src.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%' AND type IN ('table','index') "
            "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END,name"
        ).fetchall()
        if any("VIRTUAL TABLE" in sql.upper() for _, _, sql in schema):
            raise ValueError("Unexpected virtual table in business schema")
        for table in CONTENT_TABLES:
            if not src.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (table,)).fetchone():
                raise ValueError("Missing content table: " + table)
        output.mkdir(parents=True)
        with sqlite3.connect(output / "courseloop.db") as dst:
            for _, _, sql in schema:
                dst.execute(sql)
            counts = {}
            for table in CONTENT_TABLES:
                columns = [row[1] for row in src.execute("PRAGMA table_info(" + quote(table) + ")")]
                names = ",".join(map(quote, columns))
                rows = src.execute("SELECT " + names + " FROM " + quote(table))
                dst.executemany("INSERT INTO " + quote(table) + "(" + names + ") VALUES(" +
                                ",".join("?" for _ in columns) + ")", rows)
                counts[table] = dst.execute("SELECT count(*) FROM " + quote(table)).fetchone()[0]
            if dst.execute("SELECT count(*) FROM users").fetchone()[0] != 0:
                raise ValueError("Identity rows must be empty")
            dst.commit()
            dst.execute("VACUUM")
            assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        with sqlite3.connect(output / "rag_fts.db") as dst:
            ddl = rag.execute("SELECT sql FROM sqlite_master WHERE name='chunks'").fetchone()[0]
            dst.execute(ddl)
            dst.execute("CREATE TABLE chunk_hashes(hash TEXT PRIMARY KEY,course TEXT,source TEXT,chunk_index INTEGER)")
            dst.execute("CREATE TABLE build_meta(key TEXT PRIMARY KEY,value TEXT)")
            cols = "rowid," + ",".join(map(quote, FTS_COLUMNS))
            dst.executemany("INSERT INTO chunks(" + cols + ") VALUES(" + ",".join("?" for _ in range(9)) + ")",
                            rag.execute("SELECT " + cols + " FROM chunks"))
            # Rebuild derived hashes, without copying source-host build metadata.
            import re
            for text, course, name, index in dst.execute("SELECT text,course,source,chunk_index FROM chunks").fetchall():
                normalized = re.sub(r"\s+", " ", text or "").strip()
                digest = hashlib.sha256(((course or "") + "|" + normalized).encode()).hexdigest()
                dst.execute("INSERT OR IGNORE INTO chunk_hashes VALUES(?,?,?,?)", (digest, course, name, index))
            chunks = dst.execute("SELECT count(*) FROM chunks").fetchone()[0]
            by_course = dict(dst.execute("SELECT course,count(*) FROM chunks GROUP BY course"))
            dst.commit()
            dst.execute("INSERT INTO chunks(chunks) VALUES('integrity-check')")
            dst.commit()
            dst.execute("VACUUM")
            assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    manifest = {
        "profile": "processed-course-content", "attribution": ATTRIBUTION,
        "content_tables": counts, "retrieval_chunks": chunks, "chunks_by_course": by_course,
        "production_identity_rows": 0, "production_learning_rows": 0,
        "configuration_rows_copied": 0,
        "files": {p.name: {"bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                  for p in sorted(output.glob("*.db"))},
    }
    (output / "DATA_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.source, args.out), ensure_ascii=False))
