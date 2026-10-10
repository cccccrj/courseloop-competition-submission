"""应用服务旁路测试准备：只读生产白名单业务资料，不复制用户与行为数据。

必须显式 --source；仅写 courseloop-submission-test-* 新目录，绝不修改线上库。
第三方教学内容留在应用服务；此测试目录不打包/上传 GitHub。
"""
from __future__ import annotations
import argparse
import json
import os
import secrets
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    a = p.parse_args()
    if not ROOT.name.startswith("courseloop-submission-test-"):
        raise SystemExit("仅允许在独立测试环境测试目录执行")
    out = ROOT / "data/processed"
    out.mkdir(parents=True, exist_ok=True)
    target = out / "courseloop.db"
    if target.exists():
        raise SystemExit("已有测试库，拒绝覆盖")
    os.environ.update(CL_DB_PATH=str(target), CL_RAG_MODE="fts5",
                      CL_LOGS_DIR=str(ROOT / "data/logs"))
    from backend.core import db
    db.init_db()
    # 白名单包含课程内容，不包含 users / dialog_turns / 学习画像 / 路径任务。
    tables = ["courses", "app_settings", "knowledge_points", "problems", "test_cases",
              "rubric_items", "problem_knowledge"]
    src = sqlite3.connect(f"file:{a.source / 'data/processed/courseloop.db'}?mode=ro", uri=True)
    src.execute("BEGIN")
    with db.get_conn() as dest:
        for table in tables:
            columns = [r[1] for r in src.execute(f'PRAGMA table_info("{table}")')]
            cols = ",".join('"' + col + '"' for col in columns)
            rows = src.execute(f'SELECT {cols} FROM "{table}"').fetchall()
            dest.execute(f'DELETE FROM "{table}"')
            dest.executemany(f'INSERT INTO "{table}"({cols}) VALUES({",".join("?" for _ in columns)})', rows)
    src.close()
    with sqlite3.connect(f"file:{a.source / 'data/processed/rag_fts.db'}?mode=ro", uri=True) as c:
        with sqlite3.connect(out / "rag_fts.db") as d:
            c.backup(d)
    # 密钥只留在旁路机 .env（600），不进入日志/版本库/发布包。
    env_lines = [line for line in (a.source / ".env").read_text().splitlines()
                 if line.partition("=")[0].startswith("CL_LLM_")]
    env_lines += ["CL_RAG_MODE=fts5", f"CL_DB_PATH={target}",
                  f"CL_RAG_FTS_PATH={out / 'rag_fts.db'}", f"CL_RAW_DIR={ROOT / 'data/raw'}",
                  f"CL_WITHDRAWN_DIR={ROOT / 'data/withdrawn'}", f"CL_LOGS_DIR={ROOT / 'data/logs'}",
                  "CL_SECRET_KEY=" + secrets.token_hex(32)]
    (ROOT / ".env").write_text("\n".join(env_lines) + "\n")
    (ROOT / ".env").chmod(0o600)
    from scripts.submission_prepare import accounts
    fixtures = accounts()
    for row in fixtures:
        db.register_user(row["username"], row["password"], "虚构" + row["role"], role=row["role"],
                         class_name="比赛测试班" if row["role"] == "student" else "")
    for i in range(50):
        row = dict(username=f"competition-load-{i:02d}", role="student", password=secrets.token_urlsafe(14))
        db.register_user(row["username"], row["password"], f"虚构压力用户{i}", role="student",
                         class_name="并发测试班")
        fixtures.append(row)
    secret = ROOT / ".private/load-accounts.json"
    secret.write_text(json.dumps(fixtures, indent=2))
    secret.chmod(0o600)
    with db.get_conn() as c:
        n = c.execute("SELECT COUNT(*) FROM problems").fetchone()[0]
    print(json.dumps(dict(copied_content_tables=tables, problems=n, fresh_test_accounts=len(fixtures),
                          production_human_records_copied=0)))


if __name__ == "__main__":
    main()
