"""Create fictional reviewer identities in an exported processed-content copy."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def rebuild_fts(directory):
    """Normalize the derived index for the delivery SQLite, preserving content."""
    from scripts.submission_export_processed import FTS_COLUMNS
    with sqlite3.connect(directory / "rag_fts.db") as conn:
        def fingerprint():
            digest = hashlib.sha256()
            columns = "rowid," + ",".join(FTS_COLUMNS)
            for row in conn.execute("SELECT " + columns + " FROM chunks ORDER BY rowid"):
                digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode())
                digest.update(b"\n")
            return digest.hexdigest()
        before = fingerprint()
        conn.execute("INSERT INTO chunks(chunks) VALUES('rebuild')")
        conn.commit()
        conn.execute("INSERT INTO chunks(chunks) VALUES('integrity-check')")
        conn.commit()
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or fingerprint() != before:
            raise ValueError("FTS rebuild integrity/content check failed")
        conn.execute("VACUUM")
        return {"sqlite_version": sqlite3.sqlite_version, "content_sha256": before, "content_unchanged": True}


def rebuild_only(directory):
    directory = directory.resolve()
    path = directory / "DATA_MANIFEST.json"
    manifest = json.loads(path.read_text())
    if manifest.get("profile") != "processed-course-content":
        raise ValueError("Not a processed delivery copy")
    for name, info in manifest["files"].items():
        if Path(name).name != name or hashlib.sha256((directory / name).read_bytes()).hexdigest() != info["sha256"]:
            raise ValueError("Export hash mismatch")
    manifest["fts_runtime_normalization"] = rebuild_fts(directory)
    manifest["files"] = {p.name: {"bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                         for p in sorted(directory.glob("*.db"))}
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest["fts_runtime_normalization"]


def provision(directory):
    directory = directory.resolve()
    manifest_path = directory / "DATA_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("reviewer_accounts_created"):
        raise ValueError("Already provisioned; refusing overwrite")
    for name, info in manifest["files"].items():
        if Path(name).name != name or hashlib.sha256((directory / name).read_bytes()).hexdigest() != info["sha256"]:
            raise ValueError("Export hash mismatch")
    manifest["fts_runtime_normalization"] = rebuild_fts(directory)
    os.environ.update(CL_DB_PATH=str(directory / "courseloop.db"), CL_RAG_MODE="fts5",
                      CL_RAG_FTS_PATH=str(directory / "rag_fts.db"),
                      CL_LOGS_DIR=str(ROOT / ".private/processed-export-logs"))
    from backend.core import db
    from scripts.submission_prepare import accounts
    db.init_db()
    for row in accounts():
        ok, _ = db.register_user(row["username"], row["password"], "虚构" + row["role"],
                                role=row["role"], class_name="比赛演示班" if row["role"] == "student" else "")
        if not ok:
            raise ValueError("Reviewer provisioning failed")
    with db.get_conn() as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        empty = {t: conn.execute('SELECT count(*) FROM "' + t + '"').fetchone()[0]
                 for t in tables if t not in set(manifest["content_tables"]) | {"users"}}
        if any(empty.values()):
            raise ValueError("Unexpected non-content rows")
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 7
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    # Close pooled connections before compacting into a portable non-WAL file.
    with sqlite3.connect(directory / "courseloop.db") as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("VACUUM")
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    manifest["reviewer_accounts_created"] = 7
    manifest["empty_personal_and_instance_tables"] = empty
    manifest["files"] = {p.name: {"bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                         for p in sorted(directory.glob("*.db"))}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return {"problems": manifest["content_tables"]["problems"], "chunks": manifest["retrieval_chunks"],
            "reviewer_accounts": 7, "personal_rows": sum(empty.values()), "integrity": "ok"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--rebuild-fts-only", action="store_true", help="仅规范化已脱敏副本的派生索引，不改身份与内容")
    args = parser.parse_args()
    print(json.dumps(rebuild_only(args.directory) if args.rebuild_fts_only else provision(args.directory)))
