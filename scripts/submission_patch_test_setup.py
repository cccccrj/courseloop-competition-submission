"""备份上一轮纯虚构隔离库用于同数据前后对比；禁止生产库或已有目标。"""
import argparse
import os
import secrets
import sqlite3
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    args=parser.parse_args()
    assert ROOT.name.startswith("courseloop-submission-test-")
    assert args.source.name.startswith("courseloop-submission-test-")
    out=ROOT/"data/processed"
    out.mkdir(parents=True,exist_ok=True)
    assert not (out/"courseloop.db").exists()
    with sqlite3.connect(f"file:{args.source/'data/processed/courseloop.db'}?mode=ro",uri=True) as src:
        names=[r[0] for r in src.execute("SELECT username FROM users")]
        assert all(name.startswith("competition-") or name in {"demo_student","demo_ta"} for name in names)
        with sqlite3.connect(out/"courseloop.db") as dest:
            src.backup(dest)
    with sqlite3.connect(f"file:{args.source/'data/processed/rag_fts.db'}?mode=ro",uri=True) as src:
        with sqlite3.connect(out/"rag_fts.db") as dest:
            src.backup(dest)
    env="\n".join(["CL_RAG_MODE=fts5", f"CL_DB_PATH={out/'courseloop.db'}",
        f"CL_RAG_FTS_PATH={out/'rag_fts.db'}", f"CL_LOGS_DIR={ROOT/'data/logs'}",
        "CL_LLM_BASE_URL=", "CL_LLM_API_KEY=", "CL_LLM_TEXT_BASE_URL=", "CL_LLM_TEXT_API_KEY=",
        "CL_SECRET_KEY="+secrets.token_hex(32)])+"\n"
    (ROOT/".env").write_text(env)
    (ROOT/".env").chmod(0o600)
    from backend.core import db
    db.init_db()
    print("同数据备份与索引迁移完成；无生产用户与API配置。")
