"""只读盘点：不导入会启动/播种应用的模块，不输出个人信息或服务密钥。"""
from __future__ import annotations
import argparse
import hashlib
import json
import platform
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def inventory(root: Path) -> dict:
    result = {"captured_at": datetime.now(timezone.utc).isoformat(),
              "python": platform.python_version(),
              "baseline": (root / "BUILD_COMMIT").read_text().strip()
              if (root / "BUILD_COMMIT").exists() else "source checkout"}
    files = {}
    for directory in ("backend", "scripts", "tests", "frontend/src"):
        for p in sorted((root / directory).rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                files[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    result["source_sha256"] = files
    paths = {"business": root / "data/processed/courseloop.db",
             "rag": root / "data/processed/rag_fts.db"}
    for label, path in paths.items():
        if not path.exists():
            continue
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("BEGIN")  # 一次只读事务内保持计数一致，不创建线上备份
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        entry = {"integrity": conn.execute("PRAGMA quick_check").fetchone()[0],
                 "counts": {t: conn.execute('SELECT COUNT(*) FROM "' + t + '"').fetchone()[0]
                            for t in tables if not t.startswith("sqlite_")},
                 "schema": {t: [dict(zip(("ordinal", "name", "type", "not_null", "default", "pk"), r))
                                for r in conn.execute('PRAGMA table_info("' + t + '")')]
                            for t in tables if not t.startswith("sqlite_")}}
        if label == "business":
            entry["problems_by_course"] = dict(conn.execute(
                "SELECT course_id,COUNT(*) FROM problems GROUP BY course_id"))
            entry["problems_by_source"] = dict(conn.execute(
                "SELECT source,COUNT(*) FROM problems GROUP BY source"))
            entry["verification_types"] = dict(conn.execute(
                "SELECT verification_type,COUNT(*) FROM problems GROUP BY verification_type"))
            entry["users_by_role"] = dict(conn.execute(
                "SELECT role,COUNT(*) FROM users GROUP BY role"))
            entry["generated_with_citations"] = conn.execute(
                "SELECT COUNT(*) FROM problems WHERE source='generated' AND citations NOT IN ('','[]')"
            ).fetchone()[0]
        else:
            entry["chunks_by_course"] = dict(conn.execute(
                "SELECT course,COUNT(*) FROM chunks GROUP BY course"))
            entry["chunks_without_pages"] = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE page_start <= 0 OR page_end < page_start"
            ).fetchone()[0]
        result[label] = entry
        conn.close()
    # 白名单；绝不输出 URL、API_KEY、SECRET_KEY、路径或其他未审查配置。
    allowed = {"CL_RAG_MODE", "CL_LLM_WORKHORSE_MODEL", "CL_LLM_EXPERT_MODEL",
               "CL_LLM_REASONER_MODEL", "CL_LLM_VISION_MODEL", "CL_LLM_NO_THINK",
               "CL_TOP_K", "CL_RERANK_TOP_N"}
    result["safe_runtime_settings"] = {}
    env = root / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() in allowed:
                result["safe_runtime_settings"][key.strip()] = value.strip().strip("\"'")
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    a = p.parse_args()
    print(json.dumps(inventory(a.root), ensure_ascii=False, indent=2))
